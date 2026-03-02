# File: brainboost_configuration_package/BBConfig.py

import re
import os
import json
import platform
import urllib.request  # New import for URL handling
import sys
from datetime import datetime
from pathlib import Path

class BBConfig:
    
    _conf = {}
    _resolved_conf = {}
    _overrides = {}  # Dictionary to store overridden values in-memory
    # Change the default configuration file path to the URL
    # _config_file = 'https://storage.googleapis.com/brainboost_subjective_cloud_storage/global.config'
    _config_file = ''  # Set via configure() to avoid using the global config file by default.
    _upload_to_redis = False  # New flag to indicate whether to use Redis for global configuration
    _search_cache = {}
    _backup_done = False
    
    @classmethod
    def read_config(cls, force_reload=False):
        if cls._conf and not force_reload:
            return cls._conf
        cls._conf = {}
        content = ''
        if cls._config_file.startswith("http://") or cls._config_file.startswith("https://"):
            try:
                import time
                # Append a cache-busting query parameter
                cache_buster = f"?t={int(time.time())}"
                url = cls._config_file + cache_buster if "?" not in cls._config_file else cls._config_file + "&t=" + str(int(time.time()))
                req = urllib.request.Request(
                    url,
                    headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64)'}
                )
                with urllib.request.urlopen(req) as response:
                    content = response.read().decode('utf-8').splitlines()
            except Exception as e:
                raise Exception(f"Error downloading configuration from '{cls._config_file}': {str(e)}")
        else:
            try:
                with open(cls._config_file) as f:
                    content = f.readlines()
            except FileNotFoundError:
                raise FileNotFoundError(f"Configuration file '{cls._config_file}' not found.")
        
        for l in content:
            l = l.strip()
            if len(l) > 3 and not l.startswith('#'):
                if '=' in l:
                    parts = l.split('=', 1)
                    a = parts[0].strip()
                    b = parts[1].strip()
                    cls._conf[a] = b
        return cls._conf

    @classmethod
    def _candidate_roots(cls):
        roots = []
        if cls._config_file:
            cfg_dir = Path(cls._config_file).resolve().parent
            roots.append(cfg_dir)
            roots.append(cfg_dir.parent)
        roots.append(Path.cwd())
        roots.append(Path.cwd().parent)
        try:
            exe_dir = Path(sys.executable).resolve().parent
            roots.append(exe_dir)
        except Exception:
            pass
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        # De-duplicate
        seen = set()
        unique = []
        for r in roots:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        return unique

    @classmethod
    def _is_writable_dir(cls, path):
        try:
            p = Path(path)
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            test_file = p / ".bbconfig_write_test"
            test_file.write_text("ok")
            test_file.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    @classmethod
    def _default_userdata_path(cls):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return os.path.join(base, "Subjective", "com_subjective_userdata")

    @classmethod
    def _search_for_path(cls, target_name):
        """Recursively search candidate roots for a filename; cached by name."""
        if not target_name:
            return None
        name = Path(target_name).name.lower()
        if name in cls._search_cache:
            return cls._search_cache[name]
        for root in cls._candidate_roots():
            try:
                for p in root.rglob(name):
                    if p.exists():
                        cls._search_cache[name] = str(p.resolve())
                        return cls._search_cache[name]
            except (OSError, RuntimeError):
                continue
        cls._search_cache[name] = None
        return None

    @classmethod
    def _maybe_fix_path(cls, key, value):
        """If value looks like a path and does not exist, try to find it and persist."""
        if not isinstance(value, str) or not value.strip():
            return value
        # If unresolved placeholders are present, do not auto-fix (will resolve later)
        if "${" in value or "{$" in value:
            return value
        lowered = value.strip().lower()
        if lowered in ("none", "null", "false", "0"):
            return value
        # Do not auto-fix URLs/URIs
        if key.lower().endswith(("_url", "_uri")):
            return value
        if "://" in lowered or lowered.startswith("http://") or lowered.startswith("https://"):
            return value

        looks_like_path = (
            "path" in key.lower()
            or any(sep in value for sep in ("/", "\\"))
            or os.path.splitext(value)[1].lower() in {".exe", ".dll", ".so", ".dylib", ".sh", ".ps1", ".bat"}
        )
        if not looks_like_path:
            return value

        expanded = os.path.expandvars(os.path.expanduser(value))
        if os.path.isabs(expanded) and Path(expanded).exists():
            return expanded

        # Special-case user data: for relative paths, never auto-fix unless config dir is not writable
        if key.upper() == "USERDATA_PATH" and not os.path.isabs(expanded):
            cfg_dir = Path(cls._config_file).resolve().parent if cls._config_file else None
            if cfg_dir and not cls._is_writable_dir(cfg_dir):
                fallback = cls._default_userdata_path()
                # Do not persist this fallback; keep config stable across machines.
                return fallback
            return value

        # If relative path exists under known roots, keep it relative (no rewrite)
        for root in cls._candidate_roots():
            candidate = (root / expanded).resolve()
            if candidate.exists():
                return value

        found = cls._search_for_path(expanded)
        if found:
            # Prefer persisting a relative path when possible
            for root in cls._candidate_roots():
                try:
                    rel = Path(found).resolve().relative_to(root.resolve())
                    return cls._persist_fixed_path(key, str(rel))
                except Exception:
                    pass
            return cls._persist_fixed_path(key, found)

        return value

    @classmethod
    def _persist_fixed_path(cls, key, value):
        """Store resolved path in memory and on disk config file."""
        cls._conf[key] = value
        cls._resolved_conf[key] = value
        cls._write_back_config(key, value)
        try:
            print(f"Configuration key '{key}' auto-fixed to: {value}")
        except Exception:
            pass
        return value

    @classmethod
    def _autofix_all_paths(cls):
        if not cls._conf:
            return
        for k, v in list(cls._conf.items()):
            fixed = cls._maybe_fix_path(k, v)
            if fixed != v:
                cls._conf[k] = fixed

    @classmethod
    def _write_back_config(cls, key, value):
        """Update the config file in place for the given key without altering other lines."""
        if not cls._config_file or cls._config_file.startswith("http"):
            return
        cfg_path = Path(cls._config_file)
        if not cls._backup_done and cfg_path.exists():
            try:
                ts = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
                backup_path = cfg_path.with_name(f"{ts}_{cfg_path.name}.bak")
                backup_path.write_text(cfg_path.read_text())
                cls._backup_done = True
            except Exception:
                pass
        try:
            lines = cfg_path.read_text().splitlines()
        except Exception:
            return

        key_written = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={value}")
                key_written = True
            else:
                new_lines.append(line)
        if not key_written:
            new_lines.append(f"{key}={value}")
        try:
            cfg_path.write_text("\n".join(new_lines) + "\n")
        except Exception:
            # silent failure; runtime overrides still apply
            pass



    @classmethod
    def resolve_value(cls, value, seen_keys=None):
        if seen_keys is None:
            seen_keys = set()
        # Support both ${var} and {$var} syntaxes
        pattern = re.compile(r'(?:\$\{(\w+)\}|\{\$(\w+)\})')

        def replacer(match):
            key = match.group(1) or match.group(2)
            if key in seen_keys:
                raise ValueError(f"Circular reference detected for key: {key}")
            seen_keys.add(key)
            replacement = cls.get(key, resolve=True, seen_keys=seen_keys)
            seen_keys.remove(key)
            return str(replacement)

        while True:
            new_value, count = pattern.subn(replacer, value)
            if count == 0:
                break
            value = new_value
        return value 
    
    @classmethod
    def _parse_value(cls, value):
        """Parse the string value into the appropriate Python data type."""
        if not isinstance(value, str):
            return value

        value = value.strip()
        if value == 'True':
            return True
        elif value == 'False':
            return False
        elif value.isdigit():
            return int(value)
        else:
            try:
                float_val = float(value)
                return float_val
            except ValueError:
                pass
        return value

    @classmethod
    def get(cls, k, resolve=True, seen_keys=None):
        default_provided = False
        default_value = None
        # Backwards-compatible support for get(key, default) without changing signature
        if not isinstance(resolve, bool):
            default_provided = True
            default_value = resolve
            resolve = True
        # If configuration was uploaded to Redis, try to retrieve it from there.
        if cls._upload_to_redis:
            try:
                import redis
                redis_server_ip = cls._conf.get("redis_server_ip", "localhost")
                redis_server_port = int(cls._conf.get("redis_server_port", "6379"))
                r = redis.Redis(host=redis_server_ip, port=redis_server_port, db=0)
                config_str = r.get("BBConfig:global_config")
                if config_str is not None:
                    cls._conf = json.loads(config_str)
                    print("Configuration retrieved from Redis for key 'BBConfig:global_config'.")
                else:
                    print("No configuration found in Redis under key 'BBConfig:global_config'. Using local configuration.")
            except Exception as e:
                print("Error reading configuration from Redis: " + str(e))
        
        if not cls._conf:
            cls.read_config()
        
        # 1) Check if there's an override for this key
        if k in cls._overrides:
            raw_value = cls._overrides[k]
        else:
            if k not in cls._conf:
                # For specific keys, use defaults if not found.
                if k == "redis_server_ip":
                    raw_value = "localhost"
                elif k == "redis_server_port":
                    raw_value = "6379"
                elif default_provided:
                    raw_value = default_value
                else:
                    raise KeyError(f"Key '{k}' not found in configuration.")
            else:
                raw_value = cls._conf[k]
        
        if resolve and isinstance(raw_value, str):
            resolved = cls.resolve_value(raw_value, seen_keys)
        else:
            resolved = raw_value

        def _expand(value):
            if isinstance(value, str):
                return os.path.expandvars(os.path.expanduser(value))
            return value

        if isinstance(resolved, str) and ',' in resolved:
            items = [_expand(item.strip()) for item in resolved.split(',')]
            return [cls._parse_value(item) for item in items]

        resolved = _expand(resolved)
        resolved = cls._parse_value(resolved)

        # Final safeguard: if resolved value looks like a path but doesn't exist, try to auto-fix.
        resolved = cls._maybe_fix_path(k, resolved)

        return resolved
    
    @classmethod
    def sandbox(cls):
        return cls.get(k='mode') == 'sandbox'
    
    @classmethod
    def override(cls, k, value):
        if not cls._conf:
            cls.read_config()
        
        cls._overrides[k] = value
        cls._conf[k] = value
        print(f"Configuration key '{k}' overridden with value: {value}.")
        if cls._upload_to_redis:
            try:
                import redis
                redis_server_ip = cls._conf.get("redis_server_ip", "localhost")
                redis_server_port = int(cls._conf.get("redis_server_port", "6379"))
                r = redis.Redis(host=redis_server_ip, port=redis_server_port, db=0)
                r.set("BBConfig:global_config", json.dumps(cls._conf))
                print("Overridden configuration updated in Redis for key 'BBConfig:global_config'.")
            except Exception as e:
                raise Exception("Failed to update configuration in Redis: " + str(e))
    
    @classmethod
    def add_if_not_exists(cls, k, value):
        if not cls._conf:
            cls.read_config()
        
        if k not in cls._conf:
            cls._conf[k] = value
        else:
            print(f"Warning: Key '{k}' already exists in the configuration. No changes were made.")
    
    @classmethod
    def configure(cls, custom_config_path, upload_to_redis=False, redis_ip='127.0.0.1', redis_port='6379'):

        if not os.path.isfile(custom_config_path):
            raise FileNotFoundError(f"Custom configuration file '{custom_config_path}' not found.")
        
        cls._config_file = custom_config_path
        cls._conf = {}
        cls._overrides = {}
        cls.read_config()
        cls._resolved_conf = {}
        
        cls._upload_to_redis = upload_to_redis
        cls._search_cache = {}
        cls._backup_done = False

        # Inject built-in platform variables so config files can use ${DETECTED_OS}
        # and ${EXECUTABLE_FILE_EXTENSION} in paths, e.g.:
        #   some/path/${DETECTED_OS}/bin/my-tool${EXECUTABLE_FILE_EXTENSION}
        _sys = platform.system().lower()
        if _sys == "windows":
            detected_os = "windows"
            exe_ext = ".exe"
        elif _sys == "darwin":
            detected_os = "macos"
            exe_ext = ""
        else:
            detected_os = "linux"
            exe_ext = ""
        cls.add_if_not_exists(k='DETECTED_OS', value=detected_os)
        cls.add_if_not_exists(k='EXECUTABLE_FILE_EXTENSION', value=exe_ext)

        # Auto-fix missing paths on load (internal, no API change)
        cls._autofix_all_paths()
        
        if upload_to_redis:
            try:
                import redis
                # Obtain Redis connection parameters from the loaded configuration.
                cls.add_if_not_exists(k='redis_server_ip', value=redis_ip)
                cls.add_if_not_exists(k='redis_server_port', value=redis_port)

                redis_server_ip = cls.get(k='redis_server_ip')
                redis_server_port = int(cls.get(k='redis_server_port'))
                r = redis.Redis(host=redis_server_ip, port=redis_server_port, db=0)
                r.set("BBConfig:global_config", json.dumps(cls._conf))
                print("Configuration uploaded to Redis under key 'BBConfig:global_config' using Redis at {}:{}.".format(redis_server_ip, redis_server_port))
            except Exception as e:
                raise Exception("Failed to upload configuration to Redis: " + str(e))
