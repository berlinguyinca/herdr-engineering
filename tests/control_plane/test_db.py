from herdr_engineering.control_plane import db


def test_control_plane_config_defaults():
    c = db.control_plane_config({})
    assert c["postgres_dsn"].startswith("postgresql://")
    assert c["rustfs_bucket"] == "herdr-artifacts"
    assert c["rustfs_region"] == "us-east-1"


def test_control_plane_config_overrides():
    c = db.control_plane_config({"control_plane": {
        "postgres_dsn": "postgresql://x:y@h:5432/db",
        "rustfs_endpoint": "http://127.0.0.1:9000",
        "rustfs_bucket": "artifacts",
    }})
    assert c["postgres_dsn"] == "postgresql://x:y@h:5432/db"
    assert c["rustfs_endpoint"] == "http://127.0.0.1:9000"
    assert c["rustfs_bucket"] == "artifacts"
