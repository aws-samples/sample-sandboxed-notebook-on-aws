#!/usr/bin/env python3
# test-kind: offline
"""
Offline test (code-review C-B-02 / D-B-03): the proxy and pool-manager service
images contain everything their import graph needs.

  1. Static: every repo-root package imported (module- or function-level) by
     proxy/ (proxy image) or pool_manager/ + proxy/ (pool image) is COPYed by that
     Dockerfile.
  2. Dynamic: build a temp tree holding ONLY the Dockerfile's COPY sources (honouring
     .dockerignore), run `python -c 'import proxy.server'` / `import pool_manager.server`
     there with cwd and PYTHONPATH set to it, and expect exit 0.
  3. Each Dockerfile's requirements COPY source exists in the repo.

Run (sanitized env):
  env -i PATH="$PATH" HOME="$HOME" AWS_REGION=us-west-2 AWS_ACCESS_KEY_ID=testing \
    AWS_SECRET_ACCESS_KEY=testing AWS_SHARED_CREDENTIALS_FILE=/dev/null \
    AWS_CONFIG_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true \
    .venv/bin/python tests/build/test_service_images_import.py
"""
import ast
import fnmatch
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable

# image name -> (Dockerfile, code dirs scanned for imports, module to import)
IMAGES = {
    "proxy": ("proxy/Dockerfile", ["proxy"], "proxy.server"),
    "pool_manager": ("pool_manager/Dockerfile", ["pool_manager", "proxy"], "pool_manager.server"),
}

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print("FAIL:", msg)


def parse_copies(dockerfile):
    """[(source, dest)] from COPY lines (no flags/multi-source used in these files)."""
    out = []
    logical = ""
    for raw in open(os.path.join(ROOT, dockerfile), encoding="utf-8"):
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        logical += line.rstrip("\\")
        if line.endswith("\\"):
            continue
        parts = shlex.split(logical)
        logical = ""
        if parts and parts[0].upper() == "COPY":
            args = [a for a in parts[1:] if not a.startswith("--")]
            for src in args[:-1]:
                out.append((src, args[-1]))
    return out


def dockerignore_patterns():
    pats = []
    for line in open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line)
    return pats


def ignored(rel, pats):
    rel = rel.replace(os.sep, "/")
    parts = rel.split("/")
    for p in pats:
        p = p.lstrip("/") if not p.startswith("**/") else p
        if p.startswith("**/"):
            tail = p[3:]
            if any(fnmatch.fnmatch(x, tail) for x in parts):
                return True
        elif fnmatch.fnmatch(rel, p) or any(
            fnmatch.fnmatch("/".join(parts[: i + 1]), p) for i in range(len(parts))
        ):
            return True
    return False


def build_tree(dockerfile, dest_root):
    pats = dockerignore_patterns()
    for src, dst in parse_copies(dockerfile):
        sp = os.path.join(ROOT, src)
        check(os.path.exists(sp), f"{dockerfile}: COPY source {src!r} does not exist in the repo")
        if not os.path.exists(sp):
            continue
        if not dst.startswith("/app"):
            continue
        dp = os.path.join(dest_root, dst[len("/app"):].lstrip("/"))
        if os.path.isdir(sp):
            for dirpath, dirs, files in os.walk(sp):
                reld = os.path.relpath(dirpath, ROOT)
                dirs[:] = [d for d in dirs if not ignored(os.path.join(reld, d), pats)]
                for f in files:
                    relf = os.path.join(reld, f)
                    if ignored(relf, pats):
                        continue
                    t = os.path.join(dp, os.path.relpath(os.path.join(dirpath, f), sp))
                    os.makedirs(os.path.dirname(t), exist_ok=True)
                    shutil.copy2(os.path.join(dirpath, f), t)
        else:
            if dst.endswith("/"):
                dp = os.path.join(dp, os.path.basename(sp))
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            shutil.copy2(sp, dp)


def copied_top_packages(dockerfile):
    """Top-level package names a Dockerfile places directly under /app."""
    names = set()
    for src, dst in parse_copies(dockerfile):
        d = dst.rstrip("/")
        if d.startswith("/app/") and d.count("/") == 2 and os.path.isdir(os.path.join(ROOT, src)):
            names.add(d.split("/")[2])
    return names


def imported_repo_packages(dirs):
    """{package: first 'file:line'} for every import (any depth) of a repo-root directory."""
    root_pkgs = {
        d for d in os.listdir(ROOT)
        if os.path.isdir(os.path.join(ROOT, d)) and not d.startswith(".")
    }
    found = {}
    for d in dirs:
        for dirpath, subdirs, files in os.walk(os.path.join(ROOT, d)):
            subdirs[:] = [s for s in subdirs if s != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                try:
                    tree = ast.parse(open(path, encoding="utf-8").read())
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        names = [node.module.split(".")[0]]
                    for n in names:
                        if n in root_pkgs and os.path.exists(os.path.join(ROOT, n, "__init__.py")):
                            found.setdefault(n, f"{os.path.relpath(path, ROOT)}:{node.lineno}")
    return found


# The test's own sanity: the scan must see the known lazy imports.
proxy_imports = imported_repo_packages(["proxy"])
check({"app", "batch", "modelcaps"} <= set(proxy_imports),
      f"static scan sanity: expected app, batch, modelcaps in proxy imports, got {sorted(proxy_imports)}")

for image, (dockerfile, code_dirs, module) in IMAGES.items():
    copied = copied_top_packages(dockerfile)
    for pkg, where in sorted(imported_repo_packages(code_dirs).items()):
        check(pkg in copied,
              f"{dockerfile}: package '{pkg}/' is imported (first at {where}) but no "
              f"COPY {pkg}/ /app/{pkg}/ line exists (copied: {sorted(copied)})")
    for mod in code_dirs:
        check(mod in copied, f"{dockerfile}: code package '{mod}/' is not COPYed")

    tmp = tempfile.mkdtemp(prefix=f"img-{image}-")
    try:
        build_tree(dockerfile, tmp)
        env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
        env.update(PYTHONPATH=tmp, AWS_REGION="us-west-2", AWS_DEFAULT_REGION="us-west-2",
                   AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                   AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_CONFIG_FILE="/dev/null",
                   AWS_EC2_METADATA_DISABLED="true", DOCKER_CONTAINER="1",
                   PYTHONDONTWRITEBYTECODE="1", STORAGE_DB_PATH=os.path.join(tmp, "t.db"))
        r = subprocess.run([PY, "-c", f"import {module}"], cwd=tmp, env=env,
                           capture_output=True, text=True, timeout=120)
        check(r.returncode == 0,
              f"{dockerfile}: `import {module}` fails on the COPY-only tree:\n{r.stderr[-1500:]}")
        print(f"{image}: tree={sorted(os.listdir(tmp))} import {module} -> exit {r.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nALL PASS")
