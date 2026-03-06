#!/usr/bin/env python3
"""
ConfigBack - Developer Configuration Migration Tool

Back up and restore developer configuration files (pip, conda, npm, git, SSH)
across Linux, macOS, and Windows. Supports CLI and GUI interfaces.

Usage:
    configback backup [-o FILE] [-e] [-p PASS] [--include-keys] [-c CATS]
    configback restore FILE [-p PASS] [-c CATS] [--dry-run] [--force]
    configback list FILE [-p PASS]
    configback gui
"""

__version__ = "1.0.0"

import argparse
import io
import json
import logging
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

# Optional: cryptography for encryption
try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import base64
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# Optional: tkinter for GUI
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    HAS_TK = True
except ImportError:
    HAS_TK = False

logger = logging.getLogger("configback")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAGIC_HEADER = b"CFGBAK01"
SALT_LENGTH = 16
PBKDF2_ITERATIONS = 480_000

ANSI_RESET = "\033[0m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_BOLD = "\033[1m"
ANSI_CYAN = "\033[36m"


def _color(text, color):
    if sys.stdout.isatty() and os.name != "nt":
        return f"{color}{text}{ANSI_RESET}"
    return text


# ---------------------------------------------------------------------------
# Platform path detection
# ---------------------------------------------------------------------------

class PlatformPaths:
    """Resolves config file paths per operating system."""

    def __init__(self):
        self.system = sys.platform  # 'linux', 'darwin', 'win32'
        if self.system == "win32":
            self.home = Path(os.environ.get("USERPROFILE", Path.home()))
            self.appdata = Path(os.environ.get("APPDATA", self.home / "AppData" / "Roaming"))
        else:
            self.home = Path.home()
            self.appdata = None

    def _first_existing(self, *candidates):
        for p in candidates:
            if p and p.exists() and not p.is_symlink():
                return p
        # Return first candidate even if not existing (for documentation)
        return candidates[0] if candidates else None

    def resolve(self, category_id, include_keys=False):
        """Return list of (archive_path, real_path) for a category."""
        method = getattr(self, f"_resolve_{category_id}", None)
        if method is None:
            return []
        return method(include_keys=include_keys)

    # -- pip --
    def _resolve_pip(self, include_keys=False):
        items = []
        if self.system == "win32":
            pip_conf = self._first_existing(
                self.appdata / "pip" / "pip.ini",
                self.appdata / "pip" / "pip.conf",
            )
        elif self.system == "darwin":
            pip_conf = self._first_existing(
                self.home / "Library" / "Application Support" / "pip" / "pip.conf",
                self.home / ".config" / "pip" / "pip.conf",
                self.home / ".pip" / "pip.conf",
            )
        else:
            pip_conf = self._first_existing(
                self.home / ".config" / "pip" / "pip.conf",
                self.home / ".pip" / "pip.conf",
            )
        items.append(("pip/pip.conf", pip_conf))
        items.append(("pip/.pypirc", self.home / ".pypirc"))
        return items

    # -- conda --
    def _resolve_conda(self, include_keys=False):
        items = [("conda/.condarc", self.home / ".condarc")]
        return items

    # -- npm --
    def _resolve_npm(self, include_keys=False):
        return [
            ("npm/.npmrc", self.home / ".npmrc"),
            ("npm/.yarnrc", self.home / ".yarnrc"),
            ("npm/.yarnrc.yml", self.home / ".yarnrc.yml"),
        ]

    # -- git --
    def _resolve_git(self, include_keys=False):
        items = [
            ("git/.gitconfig", self.home / ".gitconfig"),
            ("git/.gitignore_global", self.home / ".gitignore_global"),
        ]
        # Also check XDG location
        xdg_ignore = self.home / ".config" / "git" / "ignore"
        if xdg_ignore.exists() and not (self.home / ".gitignore_global").exists():
            items[1] = ("git/.gitignore_global", xdg_ignore)
        return items

    # -- ssh --
    def _resolve_ssh(self, include_keys=False):
        ssh_dir = self.home / ".ssh"
        items = [
            ("ssh/config", ssh_dir / "config"),
            ("ssh/known_hosts", ssh_dir / "known_hosts"),
        ]
        if include_keys and ssh_dir.is_dir():
            for f in sorted(ssh_dir.iterdir()):
                if f.is_file() and f.name.startswith("id_") and not f.name.endswith(".pub"):
                    items.append((f"ssh/{f.name}", f))
                if f.is_file() and f.name.startswith("id_") and f.name.endswith(".pub"):
                    items.append((f"ssh/{f.name}", f))
        return items


# ---------------------------------------------------------------------------
# Conda environment helper
# ---------------------------------------------------------------------------

def _export_conda_envs():
    """Export conda environments as YAML specs. Returns list of (archive_path, yaml_str)."""
    results = []
    conda_cmd = shutil.which("conda")
    if not conda_cmd:
        logger.warning("conda not found on PATH; skipping environment export")
        return results

    try:
        proc = subprocess.run(
            [conda_cmd, "env", "list", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            logger.warning("conda env list failed: %s", proc.stderr.strip())
            return results
        env_data = json.loads(proc.stdout)
        envs = env_data.get("envs", [])
    except Exception as e:
        logger.warning("Failed to list conda environments: %s", e)
        return results

    for env_path in envs:
        env_name = os.path.basename(env_path)
        if not env_name:
            env_name = "base"
        try:
            proc = subprocess.run(
                [conda_cmd, "env", "export", "-p", env_path],
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                results.append((f"conda/envs/{env_name}.yml", proc.stdout))
        except Exception as e:
            logger.warning("Failed to export conda env '%s': %s", env_name, e)

    return results


# ---------------------------------------------------------------------------
# Encryption helper
# ---------------------------------------------------------------------------

class CryptoHelper:
    """Fernet-based encryption with PBKDF2 key derivation."""

    @staticmethod
    def is_encrypted(data):
        return data[:len(MAGIC_HEADER)] == MAGIC_HEADER

    @staticmethod
    def _derive_key(password, salt):
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=PBKDF2_ITERATIONS,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

    @staticmethod
    def encrypt(data, password):
        if not HAS_CRYPTO:
            raise RuntimeError(
                "Encryption requires the 'cryptography' package.\n"
                "Install it with: pip install cryptography"
            )
        salt = os.urandom(SALT_LENGTH)
        key = CryptoHelper._derive_key(password, salt)
        fernet = Fernet(key)
        encrypted = fernet.encrypt(data)
        header = MAGIC_HEADER + struct.pack("<I", SALT_LENGTH) + salt
        return header + encrypted

    @staticmethod
    def decrypt(data, password):
        if not HAS_CRYPTO:
            raise RuntimeError(
                "Decryption requires the 'cryptography' package.\n"
                "Install it with: pip install cryptography"
            )
        if not CryptoHelper.is_encrypted(data):
            raise ValueError("Data does not have the ConfigBack encryption header")
        offset = len(MAGIC_HEADER)
        salt_len = struct.unpack("<I", data[offset:offset + 4])[0]
        offset += 4
        salt = data[offset:offset + salt_len]
        offset += salt_len
        key = CryptoHelper._derive_key(password, salt)
        fernet = Fernet(key)
        try:
            return fernet.decrypt(data[offset:])
        except InvalidToken:
            raise ValueError("Wrong password or corrupted file")


# ---------------------------------------------------------------------------
# Backup engine
# ---------------------------------------------------------------------------

class BackupEngine:
    """Core backup, restore, and list operations."""

    ALL_CATEGORIES = ["pip", "conda", "npm", "git", "ssh"]

    CATEGORY_LABELS = {
        "pip": "PyPI / pip",
        "conda": "Conda",
        "npm": "npm / yarn",
        "git": "Git",
        "ssh": "SSH",
    }

    def __init__(self, progress_callback=None):
        self.paths = PlatformPaths()
        self.progress_callback = progress_callback or (lambda *a: None)

    def _report(self, msg):
        logger.debug(msg)
        self.progress_callback(msg)

    # -- backup --
    def backup(self, output_path, categories=None, encrypt=False, password=None,
               include_keys=False):
        categories = categories or self.ALL_CATEGORIES
        manifest = {
            "configback_version": __version__,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "platform": sys.platform,
            "hostname": socket.gethostname(),
            "encrypted": encrypt,
            "include_keys": include_keys,
            "categories": {},
        }

        buf = io.BytesIO()
        file_count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for cat in categories:
                if cat not in self.ALL_CATEGORIES:
                    self._report(f"Unknown category: {cat}, skipping")
                    continue
                cat_files = []
                # Static files
                for archive_path, real_path in self.paths.resolve(cat, include_keys):
                    if real_path and real_path.is_file():
                        try:
                            data = real_path.read_bytes()
                            zf.writestr(archive_path, data)
                            cat_files.append(archive_path)
                            file_count += 1
                            self._report(f"  Backed up: {archive_path}")
                        except PermissionError:
                            self._report(f"  Permission denied: {real_path}")
                    else:
                        logger.debug("File not found: %s", real_path)

                # Dynamic: conda environments
                if cat == "conda":
                    for archive_path, content in _export_conda_envs():
                        zf.writestr(archive_path, content.encode("utf-8"))
                        cat_files.append(archive_path)
                        file_count += 1
                        self._report(f"  Exported: {archive_path}")

                if cat_files:
                    manifest["categories"][cat] = cat_files

            # Write manifest
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        zip_bytes = buf.getvalue()

        if encrypt:
            if not password:
                raise ValueError("Password required for encryption")
            zip_bytes = CryptoHelper.encrypt(zip_bytes, password)
            if not str(output_path).endswith(".enc"):
                output_path = Path(str(output_path) + ".enc")

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(zip_bytes)

        size = output_path.stat().st_size
        self._report(f"\nBackup complete: {output_path}")
        self._report(f"  Files: {file_count}  Size: {_format_size(size)}")
        return {"path": str(output_path), "file_count": file_count, "size": size}

    # -- list --
    def list_contents(self, input_path, password=None):
        data = Path(input_path).read_bytes()
        if CryptoHelper.is_encrypted(data):
            if not password:
                raise ValueError("Archive is encrypted; password required")
            data = CryptoHelper.decrypt(data, password)

        entries = []
        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            manifest_data = zf.read("manifest.json")
            manifest = json.loads(manifest_data)
            for info in zf.infolist():
                if info.filename == "manifest.json":
                    continue
                entries.append({
                    "path": info.filename,
                    "size": info.file_size,
                    "compressed": info.compress_size,
                })
        return manifest, entries

    # -- restore --
    def restore(self, input_path, categories=None, password=None,
                dry_run=False, force=False):
        data = Path(input_path).read_bytes()
        if CryptoHelper.is_encrypted(data):
            if not password:
                raise ValueError("Archive is encrypted; password required")
            data = CryptoHelper.decrypt(data, password)

        restored = []
        skipped = []

        with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
            manifest_data = zf.read("manifest.json")
            manifest = json.loads(manifest_data)
            source_platform = manifest.get("platform", "unknown")
            self._report(f"Archive from: {source_platform} ({manifest.get('hostname', '?')})")
            self._report(f"Created: {manifest.get('timestamp', '?')}")

            for info in zf.infolist():
                if info.filename == "manifest.json":
                    continue

                parts = info.filename.split("/", 1)
                if len(parts) < 2:
                    continue
                cat = parts[0]

                # Filter by category
                if categories and cat not in categories:
                    continue

                # Skip conda env YAMLs (handled separately)
                if cat == "conda" and parts[1].startswith("envs/"):
                    if dry_run:
                        self._report(f"  [DRY-RUN] Would restore conda env: {parts[1]}")
                    else:
                        self._restore_conda_env(zf, info, force)
                    continue

                # Map archive path -> local path
                target = self._map_archive_to_local(cat, parts[1])
                if target is None:
                    self._report(f"  Cannot map: {info.filename}")
                    skipped.append(info.filename)
                    continue

                if dry_run:
                    status = "exists" if target.exists() else "new"
                    self._report(f"  [DRY-RUN] {info.filename} -> {target} ({status})")
                    restored.append(str(target))
                    continue

                # Safety: backup existing file
                if target.exists():
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    bak = target.with_name(target.name + f".bak.{ts}")
                    shutil.copy2(str(target), str(bak))
                    self._report(f"  Backed up existing: {target} -> {bak.name}")

                # Write
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info.filename))

                # Set permissions for SSH
                if cat == "ssh":
                    if "id_" in target.name and not target.name.endswith(".pub"):
                        _chmod(target, 0o600)
                    else:
                        _chmod(target, 0o644)

                self._report(f"  Restored: {info.filename} -> {target}")
                restored.append(str(target))

        self._report(f"\nRestore complete: {len(restored)} files restored, {len(skipped)} skipped")
        return {"restored": restored, "skipped": skipped}

    def _map_archive_to_local(self, category, filename):
        """Map an archive-relative filename to the local filesystem path."""
        # Resolve paths for the category and find matching filename
        for archive_path, real_path in self.paths.resolve(category, include_keys=True):
            arc_name = archive_path.split("/", 1)[1] if "/" in archive_path else archive_path
            if arc_name == filename or os.path.basename(archive_path) == filename:
                return real_path

        # Fallback: place under ~/.ssh/ for ssh, etc.
        fallback_map = {
            "pip": self.paths.home,
            "conda": self.paths.home,
            "npm": self.paths.home,
            "git": self.paths.home,
            "ssh": self.paths.home / ".ssh",
        }
        base = fallback_map.get(category)
        if base:
            return base / filename
        return None

    def _restore_conda_env(self, zf, info, force):
        """Restore a conda environment from a YAML spec."""
        conda_cmd = shutil.which("conda")
        if not conda_cmd:
            self._report(f"  conda not found; cannot restore {info.filename}")
            return
        env_name = os.path.splitext(os.path.basename(info.filename))[0]

        # Check if env exists
        try:
            proc = subprocess.run(
                [conda_cmd, "env", "list", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                existing = json.loads(proc.stdout).get("envs", [])
                for ep in existing:
                    if os.path.basename(ep) == env_name:
                        if not force:
                            self._report(f"  Conda env '{env_name}' already exists; skipping (use --force)")
                            return
        except Exception:
            pass

        # Write temp file and create env
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".yml", delete=False)
        try:
            tmp.write(zf.read(info.filename))
            tmp.close()
            self._report(f"  Creating conda env '{env_name}'...")
            proc = subprocess.run(
                [conda_cmd, "env", "create", "-f", tmp.name, "--name", env_name],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode == 0:
                self._report(f"  Conda env '{env_name}' created successfully")
            else:
                self._report(f"  Failed to create conda env '{env_name}': {proc.stderr.strip()}")
        finally:
            os.unlink(tmp.name)


def _chmod(path, mode):
    """Set file permissions (no-op on Windows)."""
    if os.name != "nt":
        try:
            os.chmod(str(path), mode)
        except OSError:
            pass


def _format_size(size_bytes):
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _default_output_path(encrypt=False):
    hostname = socket.gethostname()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = ".zip.enc" if encrypt else ".zip"
    return Path.cwd() / f"configback_{hostname}_{ts}{ext}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="configback",
        description="ConfigBack - Developer Configuration Migration Tool",
    )
    parser.add_argument("--version", action="version", version=f"configback {__version__}")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # backup
    p_backup = sub.add_parser("backup", help="Back up configuration files")
    p_backup.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    p_backup.add_argument("-o", "--output", help="Output file path (default: auto-generated)")
    p_backup.add_argument("-e", "--encrypt", action="store_true", help="Encrypt the backup")
    p_backup.add_argument("-p", "--password", help="Encryption password (interactive if omitted)")
    p_backup.add_argument("--include-keys", action="store_true",
                          help="Include SSH private keys")
    p_backup.add_argument("-c", "--categories",
                          help="Comma-separated categories: pip,conda,npm,git,ssh")

    # restore
    p_restore = sub.add_parser("restore", help="Restore configuration files from backup")
    p_restore.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    p_restore.add_argument("file", help="Backup archive path")
    p_restore.add_argument("-p", "--password", help="Decryption password")
    p_restore.add_argument("-c", "--categories",
                           help="Comma-separated categories to restore")
    p_restore.add_argument("--dry-run", action="store_true",
                           help="Show what would be restored without changes")
    p_restore.add_argument("--force", action="store_true",
                           help="Force overwrite / skip confirmations")

    # list
    p_list = sub.add_parser("list", help="List backup archive contents")
    p_list.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    p_list.add_argument("file", help="Backup archive path")
    p_list.add_argument("-p", "--password", help="Decryption password")

    # gui
    sub.add_parser("gui", help="Launch graphical interface")

    return parser


def cmd_backup(args):
    categories = args.categories.split(",") if args.categories else None
    password = None
    if args.encrypt:
        if not HAS_CRYPTO:
            print(_color("Error: Encryption requires the 'cryptography' package.", ANSI_RED))
            print("Install it with: pip install cryptography")
            return 1
        if args.password:
            password = args.password
        else:
            password = getpass("Enter encryption password: ")
            confirm = getpass("Confirm password: ")
            if password != confirm:
                print(_color("Error: Passwords do not match.", ANSI_RED))
                return 1

    output = Path(args.output) if args.output else _default_output_path(args.encrypt)
    engine = BackupEngine(progress_callback=lambda m: print(m))
    print(_color(f"ConfigBack v{__version__} - Backup", ANSI_BOLD))
    print(f"Categories: {', '.join(categories or engine.ALL_CATEGORIES)}")
    print()

    try:
        result = engine.backup(
            output_path=output,
            categories=categories,
            encrypt=args.encrypt,
            password=password,
            include_keys=args.include_keys,
        )
        print(_color("\nSuccess!", ANSI_GREEN))
        return 0
    except Exception as e:
        print(_color(f"\nError: {e}", ANSI_RED))
        return 1


def cmd_restore(args):
    input_path = Path(args.file)
    if not input_path.is_file():
        print(_color(f"Error: File not found: {input_path}", ANSI_RED))
        return 1

    categories = args.categories.split(",") if args.categories else None

    # Auto-detect encryption
    data_head = input_path.read_bytes()[:len(MAGIC_HEADER)]
    password = None
    if CryptoHelper.is_encrypted(data_head):
        if not HAS_CRYPTO:
            print(_color("Error: Archive is encrypted. Install 'cryptography' to decrypt.", ANSI_RED))
            return 1
        password = args.password or getpass("Enter decryption password: ")

    engine = BackupEngine(progress_callback=lambda m: print(m))
    print(_color(f"ConfigBack v{__version__} - Restore", ANSI_BOLD))
    if args.dry_run:
        print(_color("[DRY-RUN MODE]", ANSI_YELLOW))
    print()

    try:
        result = engine.restore(
            input_path=input_path,
            categories=categories,
            password=password,
            dry_run=args.dry_run,
            force=args.force,
        )
        print(_color("\nDone!", ANSI_GREEN))
        return 0
    except ValueError as e:
        print(_color(f"\nError: {e}", ANSI_RED))
        return 1
    except Exception as e:
        print(_color(f"\nError: {e}", ANSI_RED))
        return 1


def cmd_list(args):
    input_path = Path(args.file)
    if not input_path.is_file():
        print(_color(f"Error: File not found: {input_path}", ANSI_RED))
        return 1

    data_head = input_path.read_bytes()[:len(MAGIC_HEADER)]
    password = None
    if CryptoHelper.is_encrypted(data_head):
        if not HAS_CRYPTO:
            print(_color("Error: Archive is encrypted. Install 'cryptography' to decrypt.", ANSI_RED))
            return 1
        password = args.password or getpass("Enter decryption password: ")

    engine = BackupEngine()
    try:
        manifest, entries = engine.list_contents(input_path, password=password)
    except ValueError as e:
        print(_color(f"Error: {e}", ANSI_RED))
        return 1

    print(_color(f"ConfigBack Archive: {input_path.name}", ANSI_BOLD))
    print(f"  Version:   {manifest.get('configback_version', '?')}")
    print(f"  Created:   {manifest.get('timestamp', '?')}")
    print(f"  Platform:  {manifest.get('platform', '?')}")
    print(f"  Hostname:  {manifest.get('hostname', '?')}")
    print(f"  Encrypted: {manifest.get('encrypted', False)}")
    print()

    # Group by category
    by_cat = {}
    for entry in entries:
        parts = entry["path"].split("/", 1)
        cat = parts[0] if len(parts) > 1 else "other"
        by_cat.setdefault(cat, []).append(entry)

    for cat in sorted(by_cat):
        label = BackupEngine.CATEGORY_LABELS.get(cat, cat)
        print(_color(f"  [{label}]", ANSI_CYAN))
        for entry in by_cat[cat]:
            name = entry["path"].split("/", 1)[-1] if "/" in entry["path"] else entry["path"]
            print(f"    {name:<40} {_format_size(entry['size']):>10}")
    print()
    print(f"  Total: {len(entries)} files")
    return 0


def cmd_gui(args):
    if not HAS_TK:
        print(_color("Error: tkinter is required for GUI mode.", ANSI_RED))
        print("Install it with: sudo apt install python3-tk  (Linux)")
        return 1
    app = ConfigBackGUI()
    app.mainloop()
    return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

if HAS_TK:
    class ConfigBackGUI(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"ConfigBack v{__version__}")
            self.geometry("720x560")
            self.resizable(True, True)
            self._build_ui()

        def _build_ui(self):
            # Notebook tabs
            self.notebook = ttk.Notebook(self)
            self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))

            self._build_backup_tab()
            self._build_restore_tab()
            self._build_list_tab()

            # Log panel
            log_frame = ttk.LabelFrame(self, text="Log")
            log_frame.pack(fill="both", expand=False, padx=8, pady=8)

            self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state="disabled",
                                                       wrap="word", font=("Consolas", 9))
            self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        def _log(self, msg):
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
            self.update_idletasks()

        # -- Backup Tab --
        def _build_backup_tab(self):
            frame = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(frame, text="  Backup  ")

            # Categories
            cat_frame = ttk.LabelFrame(frame, text="Categories")
            cat_frame.pack(fill="x", pady=(0, 8))

            self.cat_vars = {}
            cat_info = [
                ("pip", "PyPI / pip  (pip.conf, .pypirc)"),
                ("conda", "Conda  (.condarc, environments)"),
                ("npm", "npm / yarn  (.npmrc, .yarnrc)"),
                ("git", "Git  (.gitconfig, .gitignore_global)"),
                ("ssh", "SSH  (config, known_hosts)"),
            ]
            for cid, label in cat_info:
                var = tk.BooleanVar(value=True)
                self.cat_vars[cid] = var
                ttk.Checkbutton(cat_frame, text=label, variable=var).pack(anchor="w", padx=8, pady=1)

            self.include_keys_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(cat_frame, text="Include SSH private keys (sensitive!)",
                            variable=self.include_keys_var).pack(anchor="w", padx=8, pady=1)

            # Encryption
            enc_frame = ttk.LabelFrame(frame, text="Encryption")
            enc_frame.pack(fill="x", pady=(0, 8))

            self.encrypt_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(enc_frame, text="Encrypt backup", variable=self.encrypt_var,
                            command=self._toggle_encrypt).pack(anchor="w", padx=8, pady=2)

            self.pw_frame = ttk.Frame(enc_frame)
            ttk.Label(self.pw_frame, text="Password:").pack(side="left", padx=(8, 4))
            self.pw_entry = ttk.Entry(self.pw_frame, show="*", width=30)
            self.pw_entry.pack(side="left", padx=4)
            ttk.Label(self.pw_frame, text="Confirm:").pack(side="left", padx=(8, 4))
            self.pw_confirm = ttk.Entry(self.pw_frame, show="*", width=30)
            self.pw_confirm.pack(side="left", padx=4)

            # Output
            out_frame = ttk.Frame(frame)
            out_frame.pack(fill="x", pady=(0, 8))
            ttk.Label(out_frame, text="Output:").pack(side="left")
            self.output_entry = ttk.Entry(out_frame)
            self.output_entry.pack(side="left", fill="x", expand=True, padx=8)
            self.output_entry.insert(0, str(_default_output_path(False)))
            ttk.Button(out_frame, text="Browse...", command=self._browse_output).pack(side="right")

            # Start button
            ttk.Button(frame, text="Start Backup", command=self._do_backup).pack(pady=8)

        def _toggle_encrypt(self):
            if self.encrypt_var.get():
                self.pw_frame.pack(fill="x", padx=8, pady=4)
                # Update output extension
                cur = self.output_entry.get()
                if not cur.endswith(".enc"):
                    self.output_entry.delete(0, "end")
                    self.output_entry.insert(0, cur + ".enc")
            else:
                self.pw_frame.pack_forget()
                cur = self.output_entry.get()
                if cur.endswith(".enc"):
                    self.output_entry.delete(0, "end")
                    self.output_entry.insert(0, cur[:-4])

        def _browse_output(self):
            path = filedialog.asksaveasfilename(
                defaultextension=".zip",
                filetypes=[("ZIP files", "*.zip"), ("Encrypted", "*.zip.enc"), ("All", "*.*")],
            )
            if path:
                self.output_entry.delete(0, "end")
                self.output_entry.insert(0, path)

        def _do_backup(self):
            cats = [c for c, v in self.cat_vars.items() if v.get()]
            if not cats:
                messagebox.showwarning("Warning", "No categories selected.")
                return
            encrypt = self.encrypt_var.get()
            password = None
            if encrypt:
                if not HAS_CRYPTO:
                    messagebox.showerror("Error",
                                         "Encryption requires 'cryptography' package.\n"
                                         "pip install cryptography")
                    return
                password = self.pw_entry.get()
                if password != self.pw_confirm.get():
                    messagebox.showerror("Error", "Passwords do not match.")
                    return
                if not password:
                    messagebox.showerror("Error", "Password cannot be empty.")
                    return

            output = self.output_entry.get().strip()
            if not output:
                messagebox.showwarning("Warning", "Please specify an output path.")
                return

            self._log("Starting backup...")
            import threading

            def _run():
                try:
                    engine = BackupEngine(progress_callback=lambda m: self.after(0, self._log, m))
                    engine.backup(
                        output_path=output,
                        categories=cats,
                        encrypt=encrypt,
                        password=password,
                        include_keys=self.include_keys_var.get(),
                    )
                    self.after(0, lambda: messagebox.showinfo("Success", f"Backup saved to:\n{output}"))
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror("Error", str(e)))
                    self.after(0, self._log, f"Error: {e}")

            threading.Thread(target=_run, daemon=True).start()

        # -- Restore Tab --
        def _build_restore_tab(self):
            frame = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(frame, text="  Restore  ")

            # Input file
            in_frame = ttk.Frame(frame)
            in_frame.pack(fill="x", pady=(0, 8))
            ttk.Label(in_frame, text="Archive:").pack(side="left")
            self.restore_entry = ttk.Entry(in_frame)
            self.restore_entry.pack(side="left", fill="x", expand=True, padx=8)
            ttk.Button(in_frame, text="Browse...", command=self._browse_restore).pack(side="right")

            # Password
            pw_frame = ttk.Frame(frame)
            pw_frame.pack(fill="x", pady=(0, 8))
            ttk.Label(pw_frame, text="Password (if encrypted):").pack(side="left")
            self.restore_pw = ttk.Entry(pw_frame, show="*", width=30)
            self.restore_pw.pack(side="left", padx=8)

            # Categories filter
            rcat_frame = ttk.LabelFrame(frame, text="Categories to restore")
            rcat_frame.pack(fill="x", pady=(0, 8))
            self.rcat_vars = {}
            for cid, label in BackupEngine.CATEGORY_LABELS.items():
                var = tk.BooleanVar(value=True)
                self.rcat_vars[cid] = var
                ttk.Checkbutton(rcat_frame, text=label, variable=var).pack(anchor="w", padx=8, pady=1)

            # Options
            opt_frame = ttk.Frame(frame)
            opt_frame.pack(fill="x", pady=(0, 8))
            self.dry_run_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(opt_frame, text="Dry run (preview only)", variable=self.dry_run_var).pack(
                anchor="w", padx=8)
            self.force_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(opt_frame, text="Force (skip confirmations)", variable=self.force_var).pack(
                anchor="w", padx=8)

            ttk.Button(frame, text="Start Restore", command=self._do_restore).pack(pady=8)

        def _browse_restore(self):
            path = filedialog.askopenfilename(
                filetypes=[("Backup files", "*.zip *.enc"), ("All", "*.*")],
            )
            if path:
                self.restore_entry.delete(0, "end")
                self.restore_entry.insert(0, path)

        def _do_restore(self):
            input_path = self.restore_entry.get().strip()
            if not input_path or not Path(input_path).is_file():
                messagebox.showwarning("Warning", "Please select a valid backup file.")
                return
            cats = [c for c, v in self.rcat_vars.items() if v.get()] or None
            password = self.restore_pw.get() or None

            self._log("Starting restore...")
            import threading

            def _run():
                try:
                    engine = BackupEngine(progress_callback=lambda m: self.after(0, self._log, m))
                    engine.restore(
                        input_path=input_path,
                        categories=cats,
                        password=password,
                        dry_run=self.dry_run_var.get(),
                        force=self.force_var.get(),
                    )
                    self.after(0, lambda: messagebox.showinfo("Done", "Restore complete."))
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror("Error", str(e)))
                    self.after(0, self._log, f"Error: {e}")

            threading.Thread(target=_run, daemon=True).start()

        # -- List Tab --
        def _build_list_tab(self):
            frame = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(frame, text="  List  ")

            # Input file
            in_frame = ttk.Frame(frame)
            in_frame.pack(fill="x", pady=(0, 8))
            ttk.Label(in_frame, text="Archive:").pack(side="left")
            self.list_entry = ttk.Entry(in_frame)
            self.list_entry.pack(side="left", fill="x", expand=True, padx=8)
            ttk.Button(in_frame, text="Browse...", command=self._browse_list).pack(side="right")

            # Password
            pw_frame = ttk.Frame(frame)
            pw_frame.pack(fill="x", pady=(0, 8))
            ttk.Label(pw_frame, text="Password (if encrypted):").pack(side="left")
            self.list_pw = ttk.Entry(pw_frame, show="*", width=30)
            self.list_pw.pack(side="left", padx=8)

            ttk.Button(frame, text="Load Contents", command=self._do_list).pack(pady=(0, 8))

            # Treeview
            cols = ("size",)
            self.tree = ttk.Treeview(frame, columns=cols, show="tree headings")
            self.tree.heading("#0", text="File / Category")
            self.tree.heading("size", text="Size")
            self.tree.column("size", width=100, anchor="e")
            self.tree.pack(fill="both", expand=True)

        def _browse_list(self):
            path = filedialog.askopenfilename(
                filetypes=[("Backup files", "*.zip *.enc"), ("All", "*.*")],
            )
            if path:
                self.list_entry.delete(0, "end")
                self.list_entry.insert(0, path)

        def _do_list(self):
            input_path = self.list_entry.get().strip()
            if not input_path or not Path(input_path).is_file():
                messagebox.showwarning("Warning", "Please select a valid backup file.")
                return
            password = self.list_pw.get() or None
            engine = BackupEngine()
            try:
                manifest, entries = engine.list_contents(input_path, password=password)
            except ValueError as e:
                messagebox.showerror("Error", str(e))
                return
            except Exception as e:
                messagebox.showerror("Error", str(e))
                return

            # Clear tree
            for item in self.tree.get_children():
                self.tree.delete(item)

            # Group by category
            by_cat = {}
            for entry in entries:
                parts = entry["path"].split("/", 1)
                cat = parts[0] if len(parts) > 1 else "other"
                by_cat.setdefault(cat, []).append(entry)

            for cat in sorted(by_cat):
                label = BackupEngine.CATEGORY_LABELS.get(cat, cat)
                parent = self.tree.insert("", "end", text=f"[{label}]", values=("",))
                for entry in by_cat[cat]:
                    name = entry["path"].split("/", 1)[-1] if "/" in entry["path"] else entry["path"]
                    self.tree.insert(parent, "end", text=name, values=(_format_size(entry["size"]),))
                self.tree.item(parent, open=True)

            info = (f"Version: {manifest.get('configback_version', '?')}  |  "
                    f"Platform: {manifest.get('platform', '?')}  |  "
                    f"Host: {manifest.get('hostname', '?')}  |  "
                    f"Files: {len(entries)}")
            self._log(info)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")

    if args.command == "backup":
        sys.exit(cmd_backup(args))
    elif args.command == "restore":
        sys.exit(cmd_restore(args))
    elif args.command == "list":
        sys.exit(cmd_list(args))
    elif args.command == "gui":
        sys.exit(cmd_gui(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
