from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
import os
import json
import requests

app = Flask(__name__)
CORS(app)

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///license.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "hawk-super-admin-2026")
RUNTIME_FETCH_TIMEOUT = int(os.environ.get("RUNTIME_FETCH_TIMEOUT", "6"))


class Store(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    store_name = db.Column(db.String(200), nullable=False)
    owner_name = db.Column(db.String(200), default="")
    phone = db.Column(db.String(100), default="")
    license_key = db.Column(db.String(200), unique=True, nullable=False, index=True)
    status = db.Column(db.String(50), default="active")
    expires_at = db.Column(db.Date, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    last_seen = db.Column(db.DateTime, nullable=True)
    last_ip = db.Column(db.String(120), default="")
    total_checks = db.Column(db.Integer, default=0)

    notes = db.Column(db.Text, default="")
    server_url = db.Column(db.String(500), default="")

    runtime_status = db.Column(db.String(80), default="unknown")
    runtime_last_seen = db.Column(db.DateTime, nullable=True)
    runtime_last_error = db.Column(db.Text, default="")
    runtime_payload = db.Column(db.Text, default="")

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
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else "",
            "last_ip": self.last_ip or "",
            "total_checks": int(self.total_checks or 0),
            "notes": self.notes or "",
            "server_url": self.server_url or "",
        }


class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.String(100), nullable=False, index=True)
    device_id = db.Column(db.String(200), nullable=False, index=True)
    device_name = db.Column(db.String(200), default="")
    last_seen = db.Column(db.DateTime, default=datetime.utcnow)
    is_blocked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    total_checks = db.Column(db.Integer, default=0)
    last_ip = db.Column(db.String(120), default="")
    app_version = db.Column(db.String(80), default="")

    def to_dict(self):
        return {
            "id": self.id,
            "store_id": self.store_id,
            "device_id": self.device_id,
            "device_name": self.device_name or "",
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "is_blocked": bool(self.is_blocked),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "total_checks": int(self.total_checks or 0),
            "last_ip": self.last_ip or "",
            "app_version": self.app_version or "",
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


def normalize_url(value):
    raw = normalize_text(value)
    if not raw:
        return ""
    if not raw.startswith("http://") and not raw.startswith("https://"):
        raw = f"http://{raw}"
    return raw.rstrip("/")


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
        store_migrations = {
            "created_at": "ALTER TABLE store ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE store ADD COLUMN updated_at DATETIME",
            "last_seen": "ALTER TABLE store ADD COLUMN last_seen DATETIME",
            "last_ip": "ALTER TABLE store ADD COLUMN last_ip VARCHAR(120) DEFAULT ''",
            "total_checks": "ALTER TABLE store ADD COLUMN total_checks INTEGER DEFAULT 0",
            "notes": "ALTER TABLE store ADD COLUMN notes TEXT DEFAULT ''",
            "server_url": "ALTER TABLE store ADD COLUMN server_url VARCHAR(500) DEFAULT ''",
            "runtime_status": "ALTER TABLE store ADD COLUMN runtime_status VARCHAR(80) DEFAULT 'unknown'",
            "runtime_last_seen": "ALTER TABLE store ADD COLUMN runtime_last_seen DATETIME",
            "runtime_last_error": "ALTER TABLE store ADD COLUMN runtime_last_error TEXT DEFAULT ''",
            "runtime_payload": "ALTER TABLE store ADD COLUMN runtime_payload TEXT DEFAULT ''",
        }

        device_migrations = {
            "created_at": "ALTER TABLE device ADD COLUMN created_at DATETIME",
            "updated_at": "ALTER TABLE device ADD COLUMN updated_at DATETIME",
            "total_checks": "ALTER TABLE device ADD COLUMN total_checks INTEGER DEFAULT 0",
            "last_ip": "ALTER TABLE device ADD COLUMN last_ip VARCHAR(120) DEFAULT ''",
            "app_version": "ALTER TABLE device ADD COLUMN app_version VARCHAR(80) DEFAULT ''",
        }

        for col, stmt in store_migrations.items():
            try:
                if not sqlite_column_exists(conn, "store", col):
                    conn.execute(text(stmt))
            except Exception as e:
                print(f"store.{col} migration skipped: {e}")

        for col, stmt in device_migrations.items():
            try:
                if not sqlite_column_exists(conn, "device", col):
                    conn.execute(text(stmt))
            except Exception as e:
                print(f"device.{col} migration skipped: {e}")

        try:
            conn.execute(text("UPDATE store SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
            conn.execute(text("UPDATE store SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
            conn.execute(text("UPDATE device SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL"))
            conn.execute(text("UPDATE device SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"))
        except Exception as e:
            print(f"timestamp backfill skipped: {e}")

        conn.commit()


def parse_runtime_payload(value):
    if isinstance(value, dict):
        return value
    raw = normalize_text(value)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def summarize_runtime_payload(runtime):
    runtime = runtime or {}
    server = runtime.get("server") if isinstance(runtime.get("server"), dict) else runtime
    license_info = runtime.get("license") if isinstance(runtime.get("license"), dict) else {}

    return {
        "status": normalize_text(server.get("status") or runtime.get("status") or "unknown") or "unknown",
        "hostname": normalize_text(server.get("hostname") or runtime.get("hostname")),
        "uptime_human": normalize_text(server.get("uptime_human") or runtime.get("uptime_human")) or "غير متوفر",
        "last_start_at": normalize_text(
            server.get("last_start_at")
            or server.get("current_started_at")
            or runtime.get("last_start_at")
            or runtime.get("current_started_at")
        ),
        "last_shutdown_at": normalize_text(
            server.get("last_shutdown_at")
            or server.get("last_stop_at")
            or runtime.get("last_shutdown_at")
            or runtime.get("last_stop_at")
        ),
        "previous_started_at": normalize_text(server.get("previous_started_at") or runtime.get("previous_started_at")),
        "request_count": int(server.get("request_count") or runtime.get("request_count") or 0),
        "failed_request_count": int(server.get("failed_request_count") or runtime.get("failed_request_count") or 0),
        "license_status": normalize_text(license_info.get("status") or server.get("license_status") or runtime.get("license_status")),
        "license_ok": license_info.get("ok") if "ok" in license_info else server.get("license_ok"),
        "app_version": normalize_text(server.get("app_version") or runtime.get("app_version")),
        "server_url": normalize_url(runtime.get("server_url") or server.get("server_url")),
        "reported_at": normalize_text(runtime.get("reported_at")),
    }


def store_runtime_to_dict(store):
    runtime = parse_runtime_payload(store.runtime_payload)
    summary = summarize_runtime_payload(runtime)
    summary.update({
        "store_id": store.store_id,
        "store_name": store.store_name,
        "server_url": normalize_url(store.server_url or summary.get("server_url")),
        "runtime_status": store.runtime_status or summary.get("status") or "unknown",
        "runtime_last_seen": store.runtime_last_seen.isoformat() if store.runtime_last_seen else "",
        "runtime_last_error": store.runtime_last_error or "",
        "raw": runtime,
    })
    return summary


def apply_runtime_report_to_store(store, runtime_payload=None, server_url="", runtime_error=""):
    if server_url:
        store.server_url = normalize_url(server_url)

    if runtime_payload:
        store.runtime_payload = json.dumps(runtime_payload, ensure_ascii=False)
        summary = summarize_runtime_payload(runtime_payload)
        store.runtime_status = summary.get("status") or "online"
        store.runtime_last_seen = datetime.utcnow()
        store.runtime_last_error = runtime_error or ""
    elif runtime_error:
        store.runtime_status = "unreachable"
        store.runtime_last_seen = datetime.utcnow()
        store.runtime_last_error = runtime_error


def fetch_store_runtime_live(store):
    target = normalize_url(store.server_url)
    if not target:
        raise RuntimeError("لا يوجد server_url لهذا المتجر")

    response = requests.get(f"{target}/api/runtime-status", timeout=RUNTIME_FETCH_TIMEOUT)
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("استجابة runtime غير صالحة")

    apply_runtime_report_to_store(store, payload, server_url=target)
    db.session.commit()
    return store_runtime_to_dict(store)


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
    app_version = normalize_text(data.get("app_version"))
    server_url = normalize_url(data.get("server_url"))
    runtime_payload = data.get("runtime") if isinstance(data.get("runtime"), dict) else None

    if not store_id or not license_key or not device_id:
        return json_error("missing required fields", 400)

    store = Store.query.filter_by(store_id=store_id, license_key=license_key).first()
    if not store:
        return json_error("invalid license", 404, status="invalid")

    final_status = get_final_store_status(store)

    remote_ip = normalize_text(
        request.headers.get("X-Forwarded-For", "").split(",")[0]
        or request.headers.get("X-Real-IP")
        or request.remote_addr
    )

    store.last_seen = datetime.utcnow()
    store.last_ip = remote_ip
    store.total_checks = int(store.total_checks or 0) + 1

    if server_url:
        store.server_url = server_url
    if runtime_payload:
        apply_runtime_report_to_store(store, runtime_payload=runtime_payload, server_url=server_url)

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

    device.last_seen = datetime.utcnow()
    device.total_checks = int(device.total_checks or 0) + 1
    device.last_ip = remote_ip
    if device_name:
        device.device_name = device_name
    if app_version:
        device.app_version = app_version

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
    notes = normalize_text(data.get("notes"))
    server_url = normalize_url(data.get("server_url"))

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
            notes=notes,
            server_url=server_url,
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


@app.route("/api/admin/update-store", methods=["POST"])
def update_store():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    data = request.get_json(silent=True) or {}
    store_id = normalize_text(data.get("store_id"))

    if not store_id:
        return json_error("missing store_id", 400)

    store = Store.query.filter_by(store_id=store_id).first()
    if not store:
        return json_error("store not found", 404)

    owner_name = data.get("owner_name")
    phone = data.get("phone")
    notes = data.get("notes")
    server_url = data.get("server_url")
    store_name = data.get("store_name")
    expires_at_raw = normalize_text(data.get("expires_at"))
    status_value = normalize_text(data.get("status")).lower()

    if store_name is not None:
        store.store_name = normalize_text(store_name) or store.store_name
    if owner_name is not None:
        store.owner_name = normalize_text(owner_name)
    if phone is not None:
        store.phone = normalize_text(phone)
    if notes is not None:
        store.notes = normalize_text(notes)
    if server_url is not None:
        store.server_url = normalize_url(server_url)
    if status_value:
        if status_value not in ["active", "suspended", "expired"]:
            return json_error("invalid status", 400)
        store.status = status_value
    if expires_at_raw:
        try:
            store.expires_at = parse_date(expires_at_raw)
        except ValueError:
            return json_error("invalid expires_at format, use YYYY-MM-DD", 400)

    store.updated_at = datetime.utcnow()
    db.session.commit()

    return json_success(
        message="store updated",
        store=store.to_dict()
    )


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

    refresh_runtime = normalize_text(request.args.get("refresh_runtime")).lower() in {"1", "true", "yes", "on"}

    stores = Store.query.order_by(Store.id.desc()).all()
    result = []

    for store in stores:
        if refresh_runtime and store.server_url:
            try:
                fetch_store_runtime_live(store)
            except Exception as exc:
                apply_runtime_report_to_store(store, runtime_error=str(exc))
                db.session.commit()

        item = store.to_dict()
        item["final_status"] = get_final_store_status(store)
        item["runtime"] = store_runtime_to_dict(store)
        item["devices_count"] = Device.query.filter_by(store_id=store.store_id).count()
        item["blocked_devices_count"] = Device.query.filter_by(store_id=store.store_id, is_blocked=True).count()

        if store.expires_at:
            item["days_remaining"] = (store.expires_at - date.today()).days
            item["expiring_soon"] = 0 <= item["days_remaining"] <= 7
        else:
            item["days_remaining"] = None
            item["expiring_soon"] = False

        result.append(item)

    return json_success(stores=result, count=len(result))


@app.route("/api/admin/store-runtime/<store_id>", methods=["GET"])
def get_store_runtime(store_id):
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    store = Store.query.filter_by(store_id=normalize_text(store_id)).first()
    if not store:
        return json_error("store not found", 404)

    force = normalize_text(request.args.get("force")).lower() in {"1", "true", "yes", "on"}
    if force and store.server_url:
        try:
            runtime = fetch_store_runtime_live(store)
            return json_success(runtime=runtime)
        except Exception as exc:
            apply_runtime_report_to_store(store, runtime_error=str(exc))
            db.session.commit()

    return json_success(runtime=store_runtime_to_dict(store))


@app.route("/api/admin/dashboard", methods=["GET"])
def dashboard():
    if not is_admin_authorized(request):
        return json_error("unauthorized", 401)

    stores = Store.query.all()
    active = 0
    suspended = 0
    expired = 0
    expiring_soon = 0
    offline_servers = 0

    for store in stores:
        final_status = get_final_store_status(store)
        if final_status == "active":
            active += 1
        elif final_status == "suspended":
            suspended += 1
        elif final_status == "expired":
            expired += 1

        if store.expires_at:
            days = (store.expires_at - date.today()).days
            if 0 <= days <= 7:
                expiring_soon += 1

        if normalize_text(store.runtime_status).lower() in {"offline", "unreachable"}:
            offline_servers += 1

    return json_success(
        stats={
            "total_stores": len(stores),
            "active_stores": active,
            "suspended_stores": suspended,
            "expired_stores": expired,
            "expiring_soon": expiring_soon,
            "devices_count": Device.query.count(),
            "offline_servers": offline_servers,
            "requests_count": int(sum((store.total_checks or 0) for store in stores)),
            "errors_count": int(sum(1 for store in stores if normalize_text(store.runtime_last_error))),
        }
    )


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
    device.updated_at = datetime.utcnow()
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
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
