from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
import os

app = Flask(__name__)
CORS(app)

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///license.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "hawk-super-admin-2026")


class Store(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    store_name = db.Column(db.String(200), nullable=False)
    owner_name = db.Column(db.String(200), default="")
    phone = db.Column(db.String(100), default="")
    license_key = db.Column(db.String(200), unique=True, nullable=False, index=True)
    status = db.Column(db.String(50), default="active")  # active / suspended / expired
    expires_at = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "store_id": self.store_id,
            "store_name": self.store_name,
            "owner_name": self.owner_name or "",
            "phone": self.phone or "",
            "license_key": self.license_key,
            "status": self.status or "active",
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.String(100), nullable=False, index=True)
    device_id = db.Column(db.String(200), nullable=False, index=True)
    device_name = db.Column(db.String(200), default="")
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    is_blocked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "store_id": self.store_id,
            "device_id": self.device_id,
            "device_name": self.device_name or "",
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "is_blocked": bool(self.is_blocked),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def json_success(**kwargs):
    payload = {"success": True}
    payload.update(kwargs)
    return jsonify(payload)


def json_error(message, status_code=400, **kwargs):
    payload = {"success": False, "message": message}
    payload.update(kwargs)
    return jsonify(payload), status_code


def normalize_text(value):
    return str(value or "").strip()


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def is_admin_authorized(req):
    token = normalize_text(req.headers.get("X-Admin-Token"))
    return token == ADMIN_TOKEN


def get_final_store_status(store):
    today = date.today()
    final_status = normalize_text(store.status).lower() or "active"

    if store.expires_at and store.expires_at < today:
        return "expired"

    return final_status


def sqlite_column_exists(conn, table_name, column_name):
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    columns = [row[1] for row in rows]
    return column_name in columns


def ensure_sqlite_schema():
    db.create_all()

    engine = db.engine
    if "sqlite" not in str(engine.url):
        return

    with engine.connect() as conn:
        try:
            if not sqlite_column_exists(conn, "store", "created_at"):
                conn.execute(text("ALTER TABLE store ADD COLUMN created_at DATETIME"))
                conn.execute(
                    text("UPDATE store SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
                )
        except Exception as e:
            print(f"store.created_at migration skipped: {e}")

        try:
            if not sqlite_column_exists(conn, "device", "created_at"):
                conn.execute(text("ALTER TABLE device ADD COLUMN created_at DATETIME"))
                conn.execute(
                    text("UPDATE device SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
                )
        except Exception as e:
            print(f"device.created_at migration skipped: {e}")

        conn.commit()


@app.route("/")
def home():
    return json_success(message="license server running")


@app.route("/api/license/check", methods=["POST"])
def check_license():
    data = request.get_json(silent=True) or {}

    store_id = normalize_text(data.get("store_id"))
    license_key = normalize_text(data.get("license_key"))
    device_id = normalize_text(data.get("device_id"))
    device_name = normalize_text(data.get("device_name"))

    if not store_id or not license_key or not device_id:
        return json_error("missing required fields", 400)

    store = Store.query.filter_by(store_id=store_id, license_key=license_key).first()
    if not store:
        return json_error("invalid license", 404, status="invalid")

    final_status = get_final_store_status(store)

    device = Device.query.filter_by(store_id=store_id, device_id=device_id).first()
    if not device:
        device = Device(
            store_id=store_id,
            device_id=device_id,
            device_name=device_name or device_id
        )
        db.session.add(device)
    else:
        device.last_seen = datetime.utcnow()
        if device_name:
            device.device_name = device_name

    db.session.commit()

    if device.is_blocked:
        return json_error(
            "this device is blocked",
            403,
            status="blocked_device"
        )

    return json_success(
        status=final_status,
        store_name=store.store_name,
        expires_at=store.expires_at.isoformat(),
        message="license checked"
    )


@app.route("/api/admin/create-store", methods=["POST"])
def create_store():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    data = request.get_json(silent=True) or {}

    store_id = normalize_text(data.get("store_id"))
    store_name = normalize_text(data.get("store_name"))
    owner_name = normalize_text(data.get("owner_name"))
    phone = normalize_text(data.get("phone"))
    license_key = normalize_text(data.get("license_key"))
    expires_at_raw = normalize_text(data.get("expires_at"))
    status_value = normalize_text(data.get("status")).lower() or "active"

    if not store_id or not store_name or not license_key or not expires_at_raw:
        return json_error("missing required fields", 400)

    if status_value not in ["active", "suspended", "expired"]:
        return json_error("invalid status", 400)

    existing_store = Store.query.filter(
        or_(Store.store_id == store_id, Store.license_key == license_key)
    ).first()

    if existing_store:
        return json_error("store_id or license_key already exists", 409)

    try:
        expires_at = parse_date(expires_at_raw)

        store = Store(
            store_id=store_id,
            store_name=store_name,
            owner_name=owner_name,
            phone=phone,
            license_key=license_key,
            status=status_value,
            expires_at=expires_at,
        )
        db.session.add(store)
        db.session.commit()

        return json_success(
            message="store created",
            store=store.to_dict()
        )
    except ValueError:
        db.session.rollback()
        return json_error("invalid expires_at format, use YYYY-MM-DD", 400)
    except IntegrityError:
        db.session.rollback()
        return json_error("store_id or license_key already exists", 409)
    except Exception as e:
        db.session.rollback()
        return json_error(str(e), 500)


@app.route("/api/admin/update-store-status", methods=["POST"])
def update_store_status():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    data = request.get_json(silent=True) or {}

    store_id = normalize_text(data.get("store_id"))
    new_status = normalize_text(data.get("status")).lower()
    expires_at_raw = normalize_text(data.get("expires_at"))

    if not store_id or new_status not in ["active", "suspended", "expired"]:
        return json_error("invalid store_id or status", 400)

    store = Store.query.filter_by(store_id=store_id).first()
    if not store:
        return json_error("store not found", 404)

    store.status = new_status

    if expires_at_raw:
        try:
            store.expires_at = parse_date(expires_at_raw)
        except ValueError:
            return json_error("invalid expires_at format, use YYYY-MM-DD", 400)

    db.session.commit()

    return json_success(
        message="store status updated",
        store_id=store.store_id,
        status=store.status,
        expires_at=store.expires_at.isoformat()
    )


@app.route("/api/admin/list-stores", methods=["GET"])
def list_stores():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    stores = Store.query.order_by(Store.id.desc()).all()
    result = []

    for store in stores:
        item = store.to_dict()
        item["final_status"] = get_final_store_status(store)
        result.append(item)

    return json_success(stores=result, count=len(result))


@app.route("/api/admin/list-devices", methods=["GET"])
def list_devices():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    store_id = normalize_text(request.args.get("store_id"))
    query = Device.query

    if store_id:
        query = query.filter_by(store_id=store_id)

    devices = query.order_by(Device.last_seen.desc()).all()

    return json_success(
        devices=[device.to_dict() for device in devices],
        count=len(devices)
    )


@app.route("/api/admin/update-device-status", methods=["POST"])
def update_device_status():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    data = request.get_json(silent=True) or {}

    store_id = normalize_text(data.get("store_id"))
    device_id = normalize_text(data.get("device_id"))
    is_blocked = data.get("is_blocked", None)

    if not store_id or not device_id or is_blocked is None:
        return json_error("missing required fields", 400)

    device = Device.query.filter_by(store_id=store_id, device_id=device_id).first()
    if not device:
        return json_error("device not found", 404)

    device.is_blocked = bool(is_blocked)
    db.session.commit()

    return json_success(
        message="device status updated",
        device=device.to_dict()
    )


@app.route("/api/admin/delete-store", methods=["POST"])
def delete_store():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    data = request.get_json(silent=True) or {}
    store_id = normalize_text(data.get("store_id"))

    if not store_id:
        return json_error("missing store_id", 400)

    store = Store.query.filter_by(store_id=store_id).first()
    if not store:
        return json_error("store not found", 404)

    Device.query.filter_by(store_id=store_id).delete()
    db.session.delete(store)
    db.session.commit()

    return json_success(message="store deleted", store_id=store_id)


with app.app_context():
    ensure_sqlite_schema()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)