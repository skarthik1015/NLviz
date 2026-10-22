"""Development set, database 2: the MySQL "employees" sample database (datacharmer/test_db) in the
dev MySQL container (database `employees`): 300k employees, 2.8M salary rows with from/to dates.

Downloads the repository archive into --download-dir (an empty directory of its own), copies it
into the container and runs the project's own loader there; then grants read access to the app's
read-only user.

    python eval/bench/datasets/load_employees.py --download-dir <empty dir> [--container nlviz-mysql-dev]
"""
from __future__ import annotations

import argparse
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ARCHIVE = "https://github.com/datacharmer/test_db/archive/refs/heads/master.zip"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-dir", required=True)
    parser.add_argument("--container", default="nlviz-mysql-dev")
    args = parser.parse_args()
    target = Path(args.download_dir)
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "test_db.zip"
    if not archive.exists():
        urllib.request.urlretrieve(ARCHIVE, archive)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    source = target / "test_db-master"

    subprocess.run(["docker", "cp", str(source), f"{args.container}:/tmp/test_db"], check=True)

    def mysql(statement: str, *flags: str) -> str:
        # The container's own MYSQL_ROOT_PASSWORD, read inside it: the password never reaches this
        # process, its command line or a traceback.
        command = f'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot {" ".join(flags)} {statement}'
        return subprocess.run(["docker", "exec", "-w", "/tmp/test_db", args.container, "sh", "-c", command],
                              capture_output=True, text=True, check=True).stdout

    mysql("< employees.sql")
    mysql("-e \"GRANT SELECT ON employees.* TO 'nlviz_ro'@'%'; FLUSH PRIVILEGES;\"")
    counts = mysql("-e \"SELECT table_name, table_rows FROM information_schema.tables "
                   "WHERE table_schema = 'employees' ORDER BY table_name\"", "-N")
    print(counts)


if __name__ == "__main__":
    main()
