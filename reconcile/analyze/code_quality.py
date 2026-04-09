"""Code quality and contribution authenticity analysis.

Classifies every diff line into a taxonomy of change types, then aggregates
per-author metrics for detecting inflated contributions, cosmetic rewrites,
and derived code.

Academic foundations:
    Fluri et al. (2007), "Change Distilling", IEEE TSE — change taxonomy
    Mockus & Votta (2000), "Identifying Reasons for Software Changes", ICSM
    Nagappan & Ball (2005), "Relative Code Churn", ICSE — churn as quality signal
    Roy, Cordy & Koschke (2009), "Clone Detection Techniques", SCP
    Bellon et al. (2007), "Comparison of Clone Detection", EMSE — thresholds
    Herzig & Zeller (2013), "Tangled Code Changes", MSR

Change taxonomy (adapted from Fluri et al. 2007):
    structural   — Logic, control flow, data structures, algorithms
    interface    — Function signatures, API contracts, imports/exports
    cosmetic     — Whitespace, formatting, brace style, line wrapping
    comment      — Comments, docstrings, annotations
    boilerplate  — Error handling templates, CORS headers, standard patterns
    literal      — String changes, numeric constants, config values
    credential   — Passwords, keys, connection strings
    vendored     — Third-party code, lock files, generated output
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field


# --- Vendor/generated path exclusion ---

VENDOR_PATTERNS = [
    # Dependency directories (any language)
    "node_modules/", "vendor/", ".venv/", "venv/", "__pycache__/",
    "site-packages/", ".gradle/", "target/", "Pods/",
    # Known third-party libraries committed to repo
    "PHPMailer/", "jquery", "bootstrap/",
    # Lock files (any language)
    "package-lock.json", "yarn.lock", "Gemfile.lock", "poetry.lock",
    "Pipfile.lock", "composer.lock", "go.sum", "Cargo.lock",
    # Build output
    "dist/", "build/", ".next/", ".nuxt/", "assets/",
    # Cache
    ".vite/", ".cache/", ".parcel-cache/",
    # System
    ".git/", ".DS_Store",
]

BINARY_EXTENSIONS = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".eot",
})


def is_vendor(path: str) -> bool:
    return any(p in path for p in VENDOR_PATTERNS)


def is_binary(path: str) -> bool:
    return any(path.endswith(ext) for ext in BINARY_EXTENSIONS)


# --- Change type constants ---

STRUCTURAL = "structural"
INTERFACE = "interface"
COSMETIC = "cosmetic"
COMMENT = "comment"
BOILERPLATE = "boilerplate"
LITERAL = "literal"
CREDENTIAL = "credential"
VENDORED = "vendored"
STYLING = "styling"


# --- Compiled classifier patterns ---

_EMPTY = re.compile(r'^\s*$')
_BRACE = re.compile(r'^\s*[{}\[\]();,]+\s*$')
_COMMENT = re.compile(r'^\s*(//|/\*|\*\s|#|<!--|\'\'\'|""")')
_CRED = re.compile(r'(\$pass\s*=|\$user\s*=|\$host\s*=|\$db\s*=|password|apikey|secret)', re.I)
_CORS = re.compile(r'header\s*\(\s*["\']Access-Control', re.I)
_VERSION_HDR = re.compile(r'X-\w+-Version', re.I)
_HTTP_CODE = re.compile(r'http_response_code\s*\(')
_JSON_ERR = re.compile(r'json_encode\s*\(.*["\'](?:error|success)["\']')
_RESPOND = re.compile(r'function\s+respond\s*\(')
_CONSOLE = re.compile(r'console\.\w+\s*\(')
_EXIT = re.compile(r'^\s*(exit|die|return)\s*[;(]')
_PREFLIGHT = re.compile(r'preflight|OPTIONS', re.I)
_IMPORT = re.compile(r'^\s*(import\s|from\s|require\s*\(|include|use\s)')
_HEADER = re.compile(r'^\s*header\s*\(')
_FUNC = re.compile(r'^\s*(function\s|const\s+\w+\s*=\s*(async\s*)?\(|def\s)')
_EXPORT = re.compile(r'^\s*export\s+(default\s+)?')
_LITERAL = re.compile(r'^[^=]*=\s*["\'][^"\']*["\']\s*;?\s*$')


def classify_line(line: str, file_path: str = "") -> str:
    """Classify a single diff line per Fluri et al. (2007) taxonomy."""
    s = line.strip()
    if is_vendor(file_path):
        return VENDORED
    if _EMPTY.match(s) or not s or s == '?>':
        return COSMETIC
    if _BRACE.match(s):
        return COSMETIC
    if _COMMENT.match(s):
        return COMMENT
    if _CRED.search(s):
        return CREDENTIAL
    if _CORS.search(s) or _VERSION_HDR.search(s):
        return BOILERPLATE
    if _HTTP_CODE.search(s):
        return BOILERPLATE
    if _JSON_ERR.search(s):
        return BOILERPLATE
    if _RESPOND.search(s) or _CONSOLE.search(s):
        return BOILERPLATE
    if _EXIT.match(s) or _PREFLIGHT.search(s):
        return BOILERPLATE
    if _IMPORT.match(s) or _HEADER.match(s):
        return INTERFACE
    if _EXPORT.match(s) or _FUNC.match(s):
        return INTERFACE
    if _LITERAL.match(s):
        return LITERAL
    # CSS files: all non-comment, non-empty content is styling/presentation,
    # not behavioral logic. Per Mockus & Votta (2000): classify by semantic purpose.
    # Fluri et al. (2007) taxonomy was designed for Java/C, not CSS.
    if file_path.endswith('.css'):
        return STYLING
    return STRUCTURAL


# --- Token-level similarity for rewrite detection (Roy et al. 2009) ---

_TRIVIAL = frozenset({
    'the', 'a', 'an', 'is', 'in', 'of', 'to', 'for', 'if', 'else',
    'return', 'echo', 'exit', 'true', 'false', 'null', 'var', 'let',
    'const', 'function', 'class', 'new', 'this', 'self',
})


def _tokenize(line: str) -> list[str]:
    line = re.sub(r'(["\'])(?:(?!\1).)*\1', 'STR', line)
    return [t.lower() for t in re.findall(r'[a-zA-Z_]\w*', line)
            if t.lower() not in _TRIVIAL and len(t) > 1]


def jaccard_similarity(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def containment_similarity(a: list[str], b: list[str]) -> float:
    """What fraction of A's tokens appear in B? (Roy et al. 2009)."""
    if not a:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa)


# Minimum fragment size for meaningful clone detection.
# Per Bellon et al. (2007) EMSE benchmark: clone pairs below 6 lines
# produce excessive false positives. We use total (del + add) >= 10
# as the threshold for "derived-modification" classification.
# Below that: "trivial-modification" regardless of token similarity.
MIN_REWRITE_LINES = 10


def detect_rewrite(
    file_path: str, deleted_lines: list[str], added_lines: list[str],
    original_author: str, current_author: str,
) -> dict:
    """Detect if modification is a derived rewrite of existing code.

    Line-weighted per Roy et al. (2009): fragment size determines significance.
    Thresholds per Bellon et al. (2007):
        total_lines < 10 → trivial-modification (below detection threshold)
        containment >= 0.6 AND jaccard >= 0.3 → derived-modification
        containment >= 0.4 → partial-derivation
        else → substantive-extension
    """
    del_tokens, add_tokens = [], []
    for line in deleted_lines:
        del_tokens.extend(_tokenize(line))
    for line in added_lines:
        add_tokens.extend(_tokenize(line))

    jacc = jaccard_similarity(del_tokens, add_tokens)
    cont = containment_similarity(del_tokens, add_tokens)
    total_lines = len(deleted_lines) + len(added_lines)

    base = {"jaccard": jacc, "containment": cont, "file": file_path,
            "original_author": original_author, "rewriter": current_author,
            "lines_deleted": len(deleted_lines), "lines_added": len(added_lines),
            "total_lines": total_lines}

    if original_author == current_author:
        return {**base, "verdict": "self-modification",
                "detail": "Author modifying their own code"}

    # Below minimum fragment size: trivial regardless of similarity
    if total_lines < MIN_REWRITE_LINES:
        return {**base, "verdict": "trivial-modification",
                "detail": (f"Below {MIN_REWRITE_LINES}-line threshold (Bellon et al. 2007). "
                           f"{total_lines} total lines. Token match not significant at this scale.")}

    if cont >= 0.6 and jacc >= 0.3:
        verdict = "derived-modification"
        detail = (f"Containment {cont:.0%}, Jaccard {jacc:.0%}, {total_lines} lines. "
                  f"Added code structurally derived from {original_author}'s implementation.")
    elif cont >= 0.4:
        verdict = "partial-derivation"
        detail = f"Containment {cont:.0%}, {total_lines} lines. Partial overlap with original structure."
    else:
        verdict = "substantive-extension"
        detail = f"Jaccard {jacc:.0%}, {total_lines} lines. Appears to be original or significantly different."

    return {**base, "verdict": verdict, "detail": detail}


# --- Commit analysis ---

@dataclass
class FileChange:
    path: str
    adds: list[tuple[str, str]] = field(default_factory=list)  # (line, type)
    dels: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class CommitAnalysis:
    sha: str
    author: str
    date: str
    message: str
    files: list[FileChange] = field(default_factory=list)
    add_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    del_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    rewrites: list[dict] = field(default_factory=list)

    @property
    def total_adds(self) -> int:
        return sum(self.add_counts.values())

    @property
    def substantive_adds(self) -> int:
        return self.add_counts.get(STRUCTURAL, 0) + self.add_counts.get(INTERFACE, 0)

    @property
    def inflation_ratio(self) -> float:
        """Non-structural / total. Higher = more inflated (Nagappan & Ball 2005)."""
        nv = self.total_adds - self.add_counts.get(VENDORED, 0)
        return 1.0 - (self.substantive_adds / nv) if nv else 0.0


def parse_git_log_patch(output: str, identity_map: dict[str, str]) -> list[CommitAnalysis]:
    """Parse `git log -p --no-merges --format=COMMIT:%H|%aN|%aI|%s` output."""
    commits: list[CommitAnalysis] = []
    current: CommitAnalysis | None = None
    current_fc: FileChange | None = None

    for line in output.split("\n"):
        if line.startswith("COMMIT:"):
            if current:
                commits.append(current)
            parts = line[7:].split("|", 3)
            if len(parts) >= 4:
                author = identity_map.get(parts[1].strip(), parts[1].strip())
                current = CommitAnalysis(sha=parts[0], author=author,
                                         date=parts[2], message=parts[3])
                current_fc = None
            continue
        if not current:
            continue
        if line.startswith("diff --git"):
            m = re.search(r"b/(.*)", line)
            if m:
                current_fc = FileChange(path=m.group(1))
                current.files.append(current_fc)
            continue
        if not current_fc:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            ctype = classify_line(content, current_fc.path)
            current_fc.adds.append((content, ctype))
            current.add_counts[ctype] += 1
        elif line.startswith("-") and not line.startswith("---"):
            content = line[1:]
            ctype = classify_line(content, current_fc.path)
            current_fc.dels.append((content, ctype))
            current.del_counts[ctype] += 1

    if current:
        commits.append(current)
    return commits


def analyze_rewrites(commits: list[CommitAnalysis]) -> list[CommitAnalysis]:
    """Detect rewrites across all commits. Builds file authorship map."""
    file_first_author: dict[str, str] = {}

    for commit in commits:
        for fc in commit.files:
            if is_vendor(fc.path):
                continue
            structural = sum(1 for _, t in fc.adds if t == STRUCTURAL)
            if fc.path not in file_first_author and structural > 3:
                file_first_author[fc.path] = commit.author
            original = file_first_author.get(fc.path)
            if original and original != commit.author and fc.dels and fc.adds:
                rw = detect_rewrite(
                    fc.path,
                    [line for line, _ in fc.dels],
                    [line for line, _ in fc.adds],
                    original, commit.author,
                )
                if rw["verdict"] != "self-modification":
                    commit.rewrites.append(rw)

    return commits


# --- Per-author profile ---

@dataclass
class AuthorProfile:
    name: str
    commits: int = 0
    structural: int = 0
    interface: int = 0
    cosmetic: int = 0
    comment: int = 0
    boilerplate: int = 0
    literal: int = 0
    credential: int = 0
    vendor: int = 0
    styling: int = 0
    total_adds: int = 0
    total_dels: int = 0
    files_touched: int = 0
    derived_modifications: int = 0
    partial_derivations: int = 0
    substantive_extensions: int = 0
    trivial_modifications: int = 0
    # Line-weighted: sum of total_lines for derived-modifications only
    derived_lines: int = 0

    @property
    def substantive(self) -> int:
        """Behavioral logic: structural + interface."""
        return self.structural + self.interface

    @property
    def feature_delivery(self) -> int:
        """Full feature contribution: structural + interface + styling.
        In a React project, JSX + CSS together = one feature."""
        return self.structural + self.interface + self.styling

    @property
    def non_vendor(self) -> int:
        return self.total_adds - self.vendor

    @property
    def feature_ratio(self) -> float:
        """Feature delivery as fraction of non-vendor adds."""
        nv = self.non_vendor
        return self.feature_delivery / nv if nv else 0.0

    @property
    def inflation_ratio(self) -> float:
        """Proportion of non-structural, non-vendor additions.
        Nagappan & Ball (2005): high relative churn = quality signal."""
        nv = self.non_vendor
        return 1.0 - (self.substantive / nv) if nv else 0.0

    @property
    def cosmetic_ratio(self) -> float:
        """Cosmetic + comment as fraction of non-vendor adds."""
        nv = self.non_vendor
        return (self.cosmetic + self.comment) / nv if nv else 0.0

    @property
    def boilerplate_ratio(self) -> float:
        """Boilerplate as fraction of non-vendor adds."""
        nv = self.non_vendor
        return self.boilerplate / nv if nv else 0.0


def aggregate_profiles(commits: list[CommitAnalysis]) -> dict[str, AuthorProfile]:
    """Build per-author quality profiles from commit analyses."""
    profiles: dict[str, AuthorProfile] = {}
    files_per_author: dict[str, set[str]] = defaultdict(set)

    for c in commits:
        a = c.author
        if a not in profiles:
            profiles[a] = AuthorProfile(name=a)
        p = profiles[a]
        p.commits += 1
        p.structural += c.add_counts.get(STRUCTURAL, 0)
        p.interface += c.add_counts.get(INTERFACE, 0)
        p.cosmetic += c.add_counts.get(COSMETIC, 0)
        p.comment += c.add_counts.get(COMMENT, 0)
        p.boilerplate += c.add_counts.get(BOILERPLATE, 0)
        p.literal += c.add_counts.get(LITERAL, 0)
        p.credential += c.add_counts.get(CREDENTIAL, 0)
        p.vendor += c.add_counts.get(VENDORED, 0)
        p.styling += c.add_counts.get(STYLING, 0)
        p.total_adds += c.total_adds
        p.total_dels += sum(c.del_counts.values())
        for fc in c.files:
            files_per_author[a].add(fc.path)
        for rw in c.rewrites:
            if rw["verdict"] == "derived-modification":
                p.derived_modifications += 1
                p.derived_lines += rw.get("total_lines", 0)
            elif rw["verdict"] == "partial-derivation":
                p.partial_derivations += 1
            elif rw["verdict"] == "substantive-extension":
                p.substantive_extensions += 1
            elif rw["verdict"] == "trivial-modification":
                p.trivial_modifications += 1

    for a, files in files_per_author.items():
        profiles[a].files_touched = len(files)

    return profiles


# ---------------------------------------------------------------------------
# Full repository analysis — shipped vs unmerged separation
# Fritz et al. (2010): authoritative contribution = deployed artifact.
# Shipped = main branch. Unmerged = branch-only effort.
# These MUST be separate pools. Mixing --all adds with main-only blame
# creates a systematic asymmetry that deflates survival for members
# with unmerged branch work.
# ---------------------------------------------------------------------------

import subprocess as _subprocess


def analyze_repository(
    repo_path: str,
    identity_map: dict[str, str],
    main_ref: str = "origin/main",
) -> dict:
    """Full two-pool analysis of a git repository.

    Returns {
        "shipped": {profiles, commits, rewrites} — main branch only,
        "unmerged": {profiles, commits} — branch-only effort,
        "all": {profiles, commits} — combined (for commit counts only),
    }

    Callers should use "shipped" for attribution and survival.
    "unmerged" is reported separately as effort-not-integrated.
    """
    # Shipped: main branch, no merges
    main_out = _subprocess.run(
        ["git", "-C", repo_path, "log", main_ref, "--no-merges", "-p",
         "--format=COMMIT:%H|%aN|%aI|%s"],
        capture_output=True, timeout=120,
    ).stdout.decode("utf-8", errors="replace")
    shipped_commits = parse_git_log_patch(main_out, identity_map)
    shipped_commits = analyze_rewrites(shipped_commits)
    shipped_profiles = aggregate_profiles(shipped_commits)
    shipped_shas = {c.sha for c in shipped_commits}

    # All branches: for identifying unmerged work
    all_out = _subprocess.run(
        ["git", "-C", repo_path, "log", "--all", "--no-merges", "-p",
         "--format=COMMIT:%H|%aN|%aI|%s"],
        capture_output=True, timeout=120,
    ).stdout.decode("utf-8", errors="replace")
    all_commits = parse_git_log_patch(all_out, identity_map)

    # Separate unmerged
    unmerged_commits = [c for c in all_commits if c.sha not in shipped_shas]
    unmerged_profiles = aggregate_profiles(unmerged_commits)

    all_profiles = aggregate_profiles(all_commits)

    return {
        "shipped": {
            "profiles": shipped_profiles,
            "commits": shipped_commits,
        },
        "unmerged": {
            "profiles": unmerged_profiles,
            "commits": unmerged_commits,
        },
        "all": {
            "profiles": all_profiles,
            "commits": all_commits,
        },
    }


# ---------------------------------------------------------------------------
# Generic stack categorizer — works on any codebase
#
# Three-layer detection, no framework-specific hardcoding:
#   Layer 1: File extension → artifact type (deterministic)
#   Layer 2: Directory name → functional role (industry conventions)
#   Layer 3: Content sniff → disambiguation (fallback only)
#
# Classification uses file extension and directory naming conventions.
# No AI/LLM content analysis. All rules are auditable lookup tables
# and regex patterns.
# ---------------------------------------------------------------------------

# Layer 1: Extension → artifact type
_EXT_MAP = {
    # Server-side code
    '.py': 'code', '.rb': 'code', '.go': 'code', '.java': 'code',
    '.rs': 'code', '.c': 'code', '.cpp': 'code', '.cs': 'code',
    '.php': 'code', '.ex': 'code', '.scala': 'code', '.kt': 'code',
    # Client-ambiguous code
    '.js': 'code', '.jsx': 'code', '.ts': 'code', '.tsx': 'code',
    '.vue': 'code', '.svelte': 'code',
    # Styling
    '.css': 'styling', '.scss': 'styling', '.less': 'styling', '.sass': 'styling',
    # Templates
    '.html': 'template', '.ejs': 'template', '.hbs': 'template',
    '.pug': 'template', '.jinja2': 'template', '.twig': 'template',
    # Database
    '.sql': 'database',
    # Scripts
    '.sh': 'script', '.bash': 'script', '.zsh': 'script',
    '.ps1': 'script', '.bat': 'script',
    # Config
    '.yml': 'config', '.yaml': 'config', '.toml': 'config',
    '.ini': 'config', '.cfg': 'config',
    '.json': 'config',
    # Documentation
    '.md': 'documentation', '.txt': 'documentation', '.rst': 'documentation',
    '.adoc': 'documentation',
}

# Layer 2: Directory name → (master, sub)
# These are industry conventions across ALL frameworks, not React-specific.
_DIR_SIGNALS = {
    # Backend
    'api': ('backend', 'api'), 'routes': ('backend', 'api'),
    'controllers': ('backend', 'api'), 'handlers': ('backend', 'api'),
    'endpoints': ('backend', 'api'), 'views': ('backend', 'api'),
    'services': ('backend', 'logic'), 'middleware': ('backend', 'logic'),
    'models': ('backend', 'database'), 'entities': ('backend', 'database'),
    'schema': ('backend', 'database'), 'migrations': ('backend', 'database'),
    'database': ('backend', 'database'),
    # Frontend
    'frontend': ('frontend', 'code'),  # generic frontend root dir
    'client': ('frontend', 'code'),    # common alt name
    'web': ('frontend', 'code'),       # another common alt
    'pages': ('frontend', 'page'), 'screens': ('frontend', 'page'),
    'components': ('frontend', 'component'), 'widgets': ('frontend', 'component'),
    'partials': ('frontend', 'component'),
    'context': ('frontend', 'core'), 'store': ('frontend', 'core'),
    'state': ('frontend', 'core'), 'redux': ('frontend', 'core'),
    'hooks': ('frontend', 'core'), 'composables': ('frontend', 'core'),
    'static': ('frontend', 'static'), 'public': ('frontend', 'static'),
    # Test
    'test': ('test', 'test'), 'tests': ('test', 'test'),
    'spec': ('test', 'test'), '__tests__': ('test', 'test'),
    # DevOps
    'scripts': ('devops', 'script'), 'bin': ('devops', 'script'),
    'deploy': ('devops', 'infra'), 'infra': ('devops', 'infra'),
    'terraform': ('devops', 'infra'), 'k8s': ('devops', 'infra'),
    '.github': ('devops', 'ci'), '.gitlab': ('devops', 'ci'),
    # Documentation
    'docs': ('maintenance', 'documentation'),
    'documentation': ('maintenance', 'documentation'),
}

# Filename overrides — universal conventions regardless of language
_FILENAME_OVERRIDES = {
    'Dockerfile': ('devops', 'infra'),
    'docker-compose.yml': ('devops', 'infra'),
    'docker-compose.yaml': ('devops', 'infra'),
    'Makefile': ('devops', 'tooling'),
    'Rakefile': ('devops', 'tooling'),
    'Justfile': ('devops', 'tooling'),
    '.htaccess': ('devops', 'hosting'),
    '.gitignore': ('devops', 'tooling'),
    '.editorconfig': ('devops', 'tooling'),
    '.gitattributes': ('devops', 'tooling'),
    'LICENSE': ('maintenance', 'documentation'),
}

# Extension → default master when no directory signal matches
_EXT_DEFAULT_MASTER = {
    'code': 'backend',      # code without directory context defaults to backend
    'styling': 'frontend',  # CSS is always frontend
    'template': 'frontend', # templates are frontend
    'database': 'backend',  # SQL is always backend
    'script': 'devops',     # shell scripts are devops
    'config': 'devops',     # config files are devops
    'documentation': 'maintenance',
}


def categorize_file(path: str, content_lines: list[str] | None = None) -> str | None:
    """Categorize a file by functional stack role.

    Three-layer detection:
      1. Extension → artifact type (deterministic)
      2. Directory names → functional role (industry conventions)
      3. Content sniff → disambiguation (fallback, no AI)

    Returns 'master:sub' or None for excluded files.
    Works on any codebase — no framework-specific hardcoding.
    All rules are auditable lookup tables and regex patterns.
    """
    # Exclusions first
    if is_vendor(path) or is_binary(path):
        return None

    # Filename overrides (Dockerfile, Makefile, .htaccess, etc.)
    basename = path.rsplit("/", 1)[-1] if "/" in path else path
    if basename in _FILENAME_OVERRIDES:
        m, s = _FILENAME_OVERRIDES[basename]
        return f"{m}:{s}"

    # Layer 3 early: content-based minified detection (physical property, not interpretation)
    if content_lines is not None:
        if content_lines and any(len(l) > 500 for l in content_lines[:3]):
            return None  # minified build output

    # Layer 1: extension → artifact type
    ext = ""
    if "." in basename:
        ext = "." + basename.rsplit(".", 1)[-1].lower()
    artifact_type = _EXT_MAP.get(ext)

    # Layer 2: scan directory segments for functional role signals
    # Scan deepest first — 'pages' is more specific than 'frontend'
    segments = path.split("/")
    dir_match = None
    for seg in reversed(segments[:-1]):  # exclude filename, deepest first
        if seg in _DIR_SIGNALS:
            dir_match = _DIR_SIGNALS[seg]
            break

    # Combine layers: artifact type can override the sub-category
    if dir_match:
        master, sub = dir_match
        # Artifact type overrides sub when it carries stronger semantic signal
        if artifact_type == 'styling':
            return "frontend:styling"
        if artifact_type == 'database':
            return "backend:database"
        if artifact_type == 'documentation':
            return "maintenance:documentation"
        if artifact_type == 'config':
            return "devops:config"
        if artifact_type == 'template':
            return f"{master}:template"
        if artifact_type == 'script':
            return "devops:script"
        return f"{master}:{sub}"

    # No directory match — use extension defaults
    if artifact_type:
        # Styling is always frontend
        if artifact_type == 'styling':
            return "frontend:styling"
        if artifact_type == 'database':
            return "backend:database"
        if artifact_type == 'documentation':
            return "maintenance:documentation"
        if artifact_type == 'script':
            return "devops:script"
        if artifact_type == 'config':
            return "devops:config"

        # Root-level code/template without directory context: legacy/orphan
        if artifact_type in ('code', 'template') and "/" not in path:
            return "legacy:orphan"

        # Code in an unrecognized directory
        if artifact_type == 'code':
            default_master = _EXT_DEFAULT_MASTER.get(artifact_type, 'other')
            return f"{default_master}:code"

        # Template
        if artifact_type == 'template':
            return "frontend:template"

    # Layer 3 fallback: shebang detection (Unix standard, not inference)
    if content_lines and content_lines[0].startswith('#!'):
        return "devops:script"

    return "other:other"
