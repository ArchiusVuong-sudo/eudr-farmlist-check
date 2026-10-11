"""Farm List Check: EUDR-style farm-list intake, geolocation checks and forest-loss overlay on PostGIS."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
RULES_DIR = ROOT / "rules"
DATA_DIR = ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
SAMPLES_DIR = DATA_DIR / "samples"
FIXTURES_DIR = ROOT / "fixtures" / "mapping"
