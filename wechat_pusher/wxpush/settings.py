"""配置加载：YAML + 环境变量占位符展开 + .env 支持。

凭证一律建议放环境变量或 `.env`，不要写进 YAML —— 配置文件太容易跟着代码一起被传走。

支持写法：
    token: "${PUSHPLUS_TOKEN}"            必需，缺失时报错
    token: "${PUSHPLUS_TOKEN:-默认值}"     缺失时用默认值

## 为什么要支持 .env（2026-09-25 踩到）

Windows 的 `setx` 只影响**此后新启动**的进程。已经开着的宿主程序（WorkBuddy 客户端、
各种 IDE、常驻服务）派生的子进程，拿到的仍是启动那一刻的环境变量快照 ——
于是出现"`setx` 明明成功了，程序还是报环境变量未生效"，而重开终端能"修好"它，
下次又莫名其妙坏掉。

把凭证写进项目根目录的 `.env`，由程序启动时自己读取，就彻底不依赖父进程环境。
优先级：**真实环境变量 > .env**（不让 .env 覆盖 CI / 系统里显式设置的值）。
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------- .env 支持

_DOTENV_LOADED = False


def project_root() -> str:
    """本包所在项目的根目录（即 wxpush/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_dotenv(path: str | None = None, *, override: bool = False) -> list[str]:
    """把 .env 里的 KEY=VALUE 注入 os.environ，返回实际注入的变量名。

    - 不传 path：依次尝试 <项目根>/.env、<当前工作目录>/.env，且只执行一次
    - 已存在于 os.environ 的变量**不覆盖**（环境变量优先）
    - 支持 `# 注释`、空行、`KEY = "值"`、行首 `export `
    """
    global _DOTENV_LOADED
    if path is None and _DOTENV_LOADED:
        return []

    candidates = [path] if path else [
        os.path.join(project_root(), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    applied: list[str] = []
    for cand in candidates:
        if not cand or not os.path.isfile(cand):
            continue
        if path is None:
            _DOTENV_LOADED = True
        try:
            with open(cand, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if not key:
                continue
            if key in os.environ and not override:
                continue
            os.environ[key] = val
            applied.append(key)
    return applied


_ENV_FULL = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}$")
_ENV_ANY = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# 记录本次加载中"引用了但没有值"的环境变量：[(变量名, 配置路径)]。
# 路径形如 channels[0].token —— 存路径是为了能把提示限定在**已启用**的通道上。
# 否则配置里那些 enabled: false 的通道，它们的 ${...} 也会被算成"缺失"，
# 于是提示让你去配一堆根本用不到的变量（实测暴露：只缺 1 个却列了 4 个）。
#
# 而为什么必须有这个提示：用户最常见的卡点不是"忘了配"，而是"配了但没生效" ——
# setx 对已打开的终端无效，必须重开终端/重新登录。此时报错若只说"缺少 webhook"，
# 用户会反复检查配置文件却找不到问题。
_MISSING_ENV: list[tuple[str, str]] = []


def _resolve(name: str, default: str | None, path: str) -> str:
    if name in os.environ:
        return os.environ[name]
    if default is None:
        _MISSING_ENV.append((name, path))
    return default if default is not None else ""


def _expand_str(value: str, path: str = "") -> str:
    m = _ENV_FULL.match(value)
    if m:
        # 整个值就是一个占位符：缺失且无默认值时返回空串，让后续"必填校验"给出准确报错
        return _resolve(m.group(1), m.group(2), path)

    def repl(match: re.Match) -> str:
        return _resolve(match.group(1), match.group(2), path)

    return _ENV_ANY.sub(repl, value)


def expand_env(obj, path: str = ""):
    """递归展开 dict / list / str 中的 ${VAR} 占位符。

    path 是内部用的：用于记录每个占位符出现在配置的哪个位置。
    """
    if isinstance(obj, dict):
        return {k: expand_env(v, f"{path}.{k}" if path else str(k))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str):
        return _expand_str(obj, path)
    return obj


def missing_env_paths() -> list[tuple[str, str]]:
    """[(变量名, 配置路径)]，去重且保序。"""
    seen, out = set(), []
    for item in _MISSING_ENV:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def missing_env() -> list[str]:
    """本次加载中引用到、但当前进程环境里没有的变量名（去重、保序）。"""
    seen, out = set(), []
    for name, _ in _MISSING_ENV:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def env_hint(names: list[str] | None = None, *,
             missing_paths: list[tuple[str, str]] | None = None,
             only_paths_prefixes: tuple[str, ...] = ()) -> str:
    """把"环境变量没生效"这件事说清楚，附带可直接复制的设置命令。

    missing_paths：用 `cfg["_missing_env_paths"]` 传进来（进程级全局统计）。
    only_paths_prefixes：只统计这些配置路径下的占位符。传进来的通常是
    `("channels[0]", "channels[2]")` 之类的**已启用通道**路径 ——
    避免把未启用通道引用到的变量也报成"缺失"。

    没有需要提示的变量时返回空串，调用方可以无条件拼接。
    """
    if names is None:
        candidates = missing_env_paths() if missing_paths is None else list(missing_paths)
        if only_paths_prefixes:
            def relevant(path: str) -> bool:
                # 不在 channels 里的占位符（全局配置）一律算相关
                if not path.startswith("channels["):
                    return True
                return any(path == pre or path.startswith(pre + ".")
                           for pre in only_paths_prefixes)
            candidates = [c for c in candidates if relevant(c[1])]
        seen, names = set(), []
        for name, _ in candidates:
            if name not in seen:
                seen.add(name)
                names.append(name)

    if not names:
        return ""
    cmds = "；".join(f'setx {n} "你的值"' for n in names)
    return (f"【环境变量未生效】{', '.join(names)} 在当前进程里读不到。"
            f"设置命令：{cmds} —— 注意 setx 对已打开的终端无效，"
            f"必须重开终端/重新登录后再运行；也可临时用 "
            f"set {names[0]}=你的值（仅当前窗口有效）；"
            f"最省事的是把值写进项目根目录的 .env（程序会自动读取，"
            f"不受 setx 生效时机影响）")


CONFIG_CANDIDATES = ("config.yaml", "config.yml", "wxpush.yaml")


def default_config() -> dict:
    """内置默认值。只有这一处，config.yaml 里没写的项从这里兜底。"""
    return {
        "network": {
            "timeout_connect": 6.0,
            "timeout_read": 15.0,
            "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            # 本机若配了 HTTP 代理且代理连不上目标域名，自动重试一次直连
            "auto_disable_proxy": True,
            "proxies": None,
        },
        "retry": {
            "attempts": 4,
            "base": 1.0,
            "cap": 30.0,
            "jitter": 0.35,
            "retry_after_cap": 120.0,
        },
        "token_cache": {
            "path": "data/tokens.json",
            "safety_margin": 300,
        },
        "dedup": {
            "enabled": False,
            "path": "data/send_dedup.json",
            "ttl_seconds": 1800,
        },
        # mode: all=所有启用通道都发(任一成功即整体成功)
        #       failover=按顺序发,第一个成功就停
        #       require_all=必须全部成功
        "mode": "all",
        "channels": [],
    }


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None, *, quiet: bool = True) -> dict:
    """加载配置。path 为空时按 CONFIG_CANDIDATES 顺序查找；都没找到则返回默认配置。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 PyYAML：pip install PyYAML") from exc

    raw: dict = {}
    resolved: str | None = None
    _MISSING_ENV.clear()   # 每次加载重新统计，避免多次加载时累加
    load_dotenv()          # 先把 .env 注入环境，再展开占位符

    if path:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"配置文件不存在: {path}")
        resolved = path
    else:
        for cand in CONFIG_CANDIDATES:
            if os.path.isfile(cand):
                resolved = cand
                break

    if resolved:
        with open(resolved, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"配置文件顶层必须是字典: {resolved}")

    cfg = _deep_merge(default_config(), expand_env(raw))
    cfg["_config_path"] = resolved
    cfg["_missing_env_paths"] = missing_env_paths()
    return cfg
