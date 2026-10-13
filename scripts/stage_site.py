"""Stage public dashboard assets with content-based filenames for browser updates."""
import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def stage(root=ROOT):
    destination = root / "_site"
    destination.mkdir(exist_ok=True)
    html = (root / "index.html").read_text()
    for filename in ("app.js", "styles.css"):
        asset = (root / filename).read_bytes()
        path = Path(filename)
        version = hashlib.sha256(asset).hexdigest()[:16]
        versioned = f"{path.stem}.{version}{path.suffix}"
        reference = f'"./{filename}"'
        if html.count(reference) != 1:
            raise ValueError(f"Expected one index reference for {filename}")
        html = html.replace(reference, f'"./{versioned}"')
        (destination / versioned).write_bytes(asset)
    (destination / "index.html").write_text(html)
    (destination / "data").mkdir(exist_ok=True)
    shutil.copyfile(root / "data" / "dashboard.json", destination / "data" / "dashboard.json")
    print("Staged dashboard with versioned JavaScript and CSS")


if __name__ == "__main__":
    stage()
