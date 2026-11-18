"""Producer releases and interface-aware delivery resolution."""

from toyyard.asset_choices import current_choice


SCHEMA = """
CREATE TABLE IF NOT EXISTS package_deliveries (
    id INTEGER PRIMARY KEY,
    package_id INTEGER NOT NULL REFERENCES packages(id),
    purpose TEXT NOT NULL,
    consumer TEXT NOT NULL,
    interface TEXT NOT NULL,
    publisher TEXT NOT NULL,
    published_on TEXT NOT NULL,
    packet_sha256 TEXT NOT NULL,
    supersedes INTEGER REFERENCES package_deliveries(id),
    compatibility TEXT NOT NULL CHECK(compatibility IN ('initial', 'compatible', 'breaking')),
    UNIQUE(package_id, purpose, consumer),
    UNIQUE(supersedes)
);
"""


def init_deliveries(db):
    db.executescript(SCHEMA)


def publish_delivery(db, package_id, *, purpose, consumer, interface, publisher, published_on,
                     packet_sha256, supersedes=None, compatibility="initial"):
    previous = None
    if not all((purpose, consumer, interface, publisher, published_on)):
        raise ValueError("A producer release requires purpose, consumer, interface, publisher and date.")
    if len(packet_sha256) != 64 or any(char not in "0123456789abcdef" for char in packet_sha256):
        raise ValueError("A producer release requires the packet SHA-256.")
    if supersedes:
        previous = db.execute("""SELECT d.* FROM package_deliveries d JOIN packages p ON p.id=d.package_id
            WHERE p.canonical_package_id=? AND d.purpose=? AND d.consumer=? AND d.publisher=?
            AND p.sample_id=(SELECT sample_id FROM packages WHERE id=?)""",
                              (supersedes, purpose, consumer, publisher, package_id)).fetchone()
        if (not previous or previous["package_id"] == package_id or compatibility not in ("compatible", "breaking")
                or (compatibility == "compatible") != (interface == previous["interface"])):
            raise ValueError("Replacement must name a prior delivery in this scope and declare interface compatibility.")
    elif compatibility != "initial":
        raise ValueError("Replacement policy requires a prior delivery.")
    record = (package_id, purpose, consumer, interface, publisher, published_on, packet_sha256,
              previous["id"] if previous else None, compatibility)
    with db:
        db.execute("""INSERT INTO package_deliveries
            (package_id,purpose,consumer,interface,publisher,published_on,packet_sha256,supersedes,compatibility)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(package_id,purpose,consumer) DO NOTHING""", record)
        actual = db.execute("""SELECT package_id,purpose,consumer,interface,publisher,published_on,packet_sha256,supersedes,compatibility
            FROM package_deliveries WHERE package_id=? AND purpose=? AND consumer=?""", record[:3]).fetchone()
        if tuple(actual) != record:
            raise ValueError("A published delivery is immutable. Publish a new package version.")


def resolve_delivery(db, purpose, consumer, interface, version=None):
    choice = current_choice(db, purpose) if version is None else None
    selected = version or (choice["canonical_package_id"] if choice else None)
    if not selected:
        return None
    base = """SELECT d.*, p.canonical_package_id, s.canonical_sample_id FROM package_deliveries d
        JOIN packages p ON p.id=d.package_id JOIN samples s ON s.id=p.sample_id WHERE """
    row = db.execute(base + "d.purpose=? AND d.consumer=? AND p.canonical_package_id=?",
                     (purpose, consumer, selected)).fetchone()
    if version is None:
        while row:
            newer = db.execute(base + "d.supersedes=? AND d.compatibility='compatible'", (row["id"],)).fetchone()
            if not newer:
                break
            row = newer
    return row if row and row["interface"] == interface else None
