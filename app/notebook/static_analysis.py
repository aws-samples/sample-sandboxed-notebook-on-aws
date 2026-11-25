"""
Static def-use analysis for notebook cells — dependency-free (stdlib only).

Part of: app.notebook (shared analysis layer)

This is the SINGLE SOURCE OF TRUTH for extracting a cell's variable READS and
WRITES from its SOURCE CODE ALONE (no execution, no namespace). It powers two
things that must agree:

  1. Runtime def-use: the SandboxExecutor imports `referenced_names` /
     `assigned_targets` and, after running a cell, reports the reads that actually
     resolve in the namespace (execution-accurate).

  2. The persisted notebook DAG: the proxy's pure DAG builder
     (proxy/notebook/dag.py) calls `analyze_cell` for every cell whose code hash
     changed, so the dependency graph exists for a freshly-opened notebook BEFORE
     any VM runs it. Once cells actually run, the captured runtime facts replace
     these static estimates in the DAG document. `dynamic_name_uses` flags cells
     whose names cannot be resolved statically (exec/eval/globals()/…), and
     `derive_sql_output_variable` mirrors the web's default SQL variable name.

Reads/writes are computed conservatively (erring toward reporting a read): an extra
edge is a harmless visual cue; a missed one just under-marks. Only stdlib `ast`
(Python cells) and `re` (SQL cells) are used, so the proxy can import this without
pulling in any VM-runtime dependencies.
"""

import ast
import os
import re


# ─── In-place mutation (shared by assigned_targets and mutated_names) ──────────

# Methods that change their receiver in place. Deliberately limited to methods
# that are in-place on the common containers (list/dict/set/DataFrame/Series):
# `x.append(…)`, `d.update(…)`, `df.insert(…)`, `df.pop('c')`. Any method called
# with `inplace=True` (pandas: sort_values, reset_index, fillna, drop, …) counts too.
_MUTATING_METHODS = frozenset({
    "append", "extend", "insert", "update", "pop", "popitem", "remove", "clear",
    "sort", "reverse", "setdefault", "add", "discard",
})


def _base_name(node):
    """`df['a'].b[0]` → 'df' (the Name at the root of a subscript/attribute chain)."""
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_inplace_call(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    if call.func.attr in _MUTATING_METHODS:
        return True
    return any(kw.arg == "inplace" and isinstance(kw.value, ast.Constant) and kw.value.value is True
               for kw in call.keywords)


def _inplace_bases(tree) -> set:
    """Names whose EXISTING object a cell changes in place at module scope:
    subscript/attribute assignment or augmented assignment (`df['c'] = …`,
    `df.attr = …`, `df['c'] += …`), `del df['c']` / `del obj.attr`, and in-place
    method calls (`df.sort_values(…, inplace=True)`, `items.append(x)`).
    Nested function/lambda/class bodies are skipped: they only run when called."""
    names: set = set()
    stack = list(tree.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        targets = []
        if isinstance(n, ast.Assign):
            targets = list(n.targets)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        elif isinstance(n, ast.Delete):
            targets = list(n.targets)
        for t in targets:
            for sub in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
                if isinstance(sub, (ast.Subscript, ast.Attribute)):
                    b = _base_name(sub)
                    if b:
                        names.add(b)
        if isinstance(n, ast.Call) and _is_inplace_call(n):
            b = _base_name(n.func.value)
            if b:
                names.add(b)
        stack.extend(ast.iter_child_nodes(n))
    return names


# ─── Python cell analysis (AST) ───────────────────────────────────────────────

def assigned_targets(code: str) -> set:
    """Names a cell assigns at the top level (via AST). Covers `x =`, `x += `,
    `x: T = `, walrus, `for x in`, `with ... as x`, imports, def/class, the
    base object of subscript/attribute assigns (`df['c'] = `, `df.col = ` → `df`)
    and of other in-place mutations (`del df['c']`, `df.sort_values(…,
    inplace=True)`, `items.append(x)` → `df` / `items`; see _inplace_bases).

    The executor folds these into a run's "modified" set, so a cell that only
    mutates a frame in place still becomes its provenance `last_cell`."""
    names: set = set()

    def _collect(target):
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for e in target.elts:
                _collect(e)
        elif isinstance(target, ast.Starred):
            _collect(target.value)
        elif isinstance(target, (ast.Subscript, ast.Attribute)):
            base = target
            while isinstance(base, (ast.Subscript, ast.Attribute)):
                base = base.value
            if isinstance(base, ast.Name):
                names.add(base.id)

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _collect(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            _collect(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _collect(node.target)
        elif isinstance(node, ast.comprehension):
            _collect(node.target)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                _collect(node.optional_vars)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    names |= _inplace_bases(tree)
    return names


def mutated_names(code: str) -> set:
    """Variables this cell changes IN PLACE that it did not create itself: the
    in-place targets of _inplace_bases (plus `x += …` on a name) that are also
    free reads of the cell. `products360['action'] = …` in a cell that reads
    products360 → {'products360'}; a cell that builds `df = …` and then sets
    `df['c'] = …` creates df, so df is not "mutated" there.

    The dependency graph treats a mutated variable as read-AND-written: the cell
    depends on the variable's previous producer and becomes its producer for the
    cells after it (web/src/services/depGraph.js)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    touched = _inplace_bases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            touched.add(node.target.id)
    return touched & referenced_names(code)


def rebound_names(code: str) -> set:
    """Names this cell REBINDS to a fresh object at the top level — i.e. a real
    (re)definition like `x = ...`, `x: T = ...`, `x := ...`, `import x`, `def x`,
    `class x`, `for x in ...`, `with ... as x`.

    This is the "definition" signal for variable provenance ("Defined in cell X").
    It is deliberately NARROWER than `assigned_targets`: it EXCLUDES in-place writes
    through a subscript or attribute (`df['c'] = ...`, `df.col = ...`), which mutate
    an existing object without redefining the name. Those must NOT reset a variable's
    `defined_by`, or an in-place edit cell would steal ownership from the cell that
    actually created the variable. Tuple/list/starred unpacking targets are included
    (each bound Name is a rebind)."""
    names: set = set()

    def _collect(target):
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for e in target.elts:
                _collect(e)
        elif isinstance(target, ast.Starred):
            _collect(target.value)
        # Subscript/Attribute targets are in-place mutations, NOT rebinds — skip.

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                _collect(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            _collect(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _collect(node.target)
        elif isinstance(node, ast.comprehension):
            _collect(node.target)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                _collect(node.optional_vars)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
    return names


def referenced_names(code: str) -> set:
    """Free variables a cell READS from the shared namespace — the "use" half of
    a def-use graph (the "def" half is provenance). A free variable is a name the
    cell loads but does not bind itself, computed scope-aware so a name that is
    local to a nested function/lambda/comprehension does NOT cancel a genuine
    module-level read of the same name (and vice-versa).

    Method: recurse the AST tracking a stack of bound-name sets (one frame per
    scope). At each `Load` of an `ast.Name`, if the name is not bound in ANY
    enclosing frame, it's free → a read. Attributes/subscripts contribute only
    their base name (`df.col`, `df['c']` → read `df`). This intentionally errs
    toward reporting reads (Tier 1 only MARKS stale; it never auto-runs, so an
    extra edge is a harmless visual cue, a missed one just under-marks)."""
    reads: set = set()

    try:
        tree = ast.parse(code)
    except SyntaxError:
        return reads

    def _target_names(target, into: set):
        """Names bound by an assignment/loop/with target (mirror of _collect,
        but for the base of subscript/attribute we do NOT bind — `df['c']=` reads
        df, it doesn't rebind it; that base is handled as a Load elsewhere)."""
        if isinstance(target, ast.Name):
            into.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for e in target.elts:
                _target_names(e, into)
        elif isinstance(target, ast.Starred):
            _target_names(target.value, into)
        # Subscript/Attribute targets are NOT bindings of their base name.

    def _params(args: ast.arguments, into: set):
        for a in (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)):
            into.add(a.arg)
        if args.vararg:
            into.add(args.vararg.arg)
        if args.kwarg:
            into.add(args.kwarg.arg)

    def _bindings_of_scope(nodes) -> set:
        """Names bound directly within a scope's body (not descending into nested
        function/class scopes, which have their own frames)."""
        bound: set = set()
        stack = list(nodes)
        while stack:
            n = stack.pop()
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    _target_names(t, bound)
            elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)):
                _target_names(n.target, bound)
            # NOTE: AugAssign (`x += ...`) is deliberately NOT treated as a
            # binding here. `x` must pre-exist, so a cell whose ONLY mention of x
            # is `x += ...` reads x from another cell. If the cell ALSO has a
            # plain `x = ...`, that Assign binds it (handled above) and the
            # augassign read is then correctly filtered out as cell-local.
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                _target_names(n.target, bound)
            elif isinstance(n, ast.withitem):
                if n.optional_vars is not None:
                    _target_names(n.optional_vars, bound)
            elif isinstance(n, ast.ExceptHandler):
                if n.name:
                    bound.add(n.name)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
                continue  # do NOT descend — nested scope handled separately
            elif isinstance(n, (ast.Lambda,)):
                continue  # its own scope
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for alias in n.names:
                    if alias.name == "*":
                        continue
                    bound.add((alias.asname or alias.name).split(".")[0])
            # descend into children that share this scope
            for child in ast.iter_child_nodes(n):
                stack.append(child)
        return bound

    def visit(node, scopes):
        """scopes: list of bound-name frames, innermost last."""
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                name = node.id
                if not any(name in frame for frame in scopes):
                    reads.add(name)
            return
        if isinstance(node, ast.AugAssign):
            # `x += ...` READS x's prior value AND writes it. The target has Store
            # ctx (so the Name branch won't see it). Count it as a read ONLY when
            # x is a free variable — i.e. NOT also bound elsewhere in this cell.
            base = node.target
            while isinstance(base, (ast.Subscript, ast.Attribute)):
                base = base.value
            if isinstance(base, ast.Name) and not any(base.id in frame for frame in scopes):
                reads.add(base.id)
            visit(node.value, scopes)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # New scope: params + names bound in its body.
            frame = set()
            _params(node.args, frame)
            body = node.body if isinstance(node.body, list) else [node.body]
            frame |= _bindings_of_scope(body)
            # Default values / decorators / annotations are evaluated in the
            # ENCLOSING scope — visit them without the new frame.
            for d in getattr(node, "decorator_list", []):
                visit(d, scopes)
            for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
                visit(default, scopes)
            for sub in body:
                visit(sub, scopes + [frame])
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            frame = set()
            for gen in node.generators:
                _target_names(gen.target, frame)
            # The first iterable is evaluated in the ENCLOSING scope; the rest and
            # the element/conditions see the comprehension targets.
            for i, gen in enumerate(node.generators):
                if i == 0:
                    visit(gen.iter, scopes)
                else:
                    visit(gen.iter, scopes + [frame])
                for cond in gen.ifs:
                    visit(cond, scopes + [frame])
            inner = scopes + [frame]
            if isinstance(node, ast.DictComp):
                visit(node.key, inner)
                visit(node.value, inner)
            else:
                visit(node.elt, inner)
            return
        # Default: recurse over children in the same scope.
        for child in ast.iter_child_nodes(node):
            visit(child, scopes)

    module_frame = _bindings_of_scope(list(tree.body))
    for stmt in tree.body:
        visit(stmt, [module_frame])
    return reads


# ─── SQL cell analysis (regex) ─────────────────────────────────────────────────

# {identifier} scalar placeholders — mirrors sql_engine._PLACEHOLDER_RE. These are
# interpolated from a Python cell's scalar variable, so each is a cross-cell READ.
_SQL_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")

# Bare identifier after FROM/JOIN — a potential in-memory DataFrame source (READ).
# Mirrors the DATAFRAME branch of sql_engine._classify_sources: excludes function
# calls (read_csv(...)) and dotted refs (db.table / dynamodb."t"). We can't confirm
# it's a DataFrame without a namespace, so this is a best-effort static read that
# the runtime pass will confirm or drop.
_SQL_FROM_BARE_RE = re.compile(
    r'(?:FROM|JOIN)\s+([A-Za-z_]\w*)\b(?!\s*\()(?!\s*\.)',
    re.IGNORECASE,
)

_SQL_KEYWORDS = {
    'select', 'from', 'join', 'where', 'group', 'order', 'having', 'limit',
    'offset', 'union', 'intersect', 'except', 'as', 'on', 'and', 'or', 'not',
    'in', 'between', 'like', 'dynamodb', 'read_csv', 'read_json', 'read_parquet',
}


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r'--[^\n]*', '', sql)
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
    return sql


def sql_reads(sql: str) -> set:
    """Cross-cell reads a SQL cell depends on: {var} scalar placeholders plus bare
    FROM/JOIN identifiers that could be in-memory DataFrames."""
    clean = _strip_sql_comments(sql)
    reads = set(_SQL_PLACEHOLDER_RE.findall(clean))
    for m in _SQL_FROM_BARE_RE.finditer(clean):
        ref = m.group(1)
        if ref.lower() not in _SQL_KEYWORDS:
            reads.add(ref)
    return reads


# ─── Unified per-cell analysis ─────────────────────────────────────────────────

def analyze_cell(cell_type: str, code: str, output_variable: str = "result") -> dict:
    """Static reads/writes for one cell.

    - Python (code) cell: AST free-vars (reads) + assignment targets (writes).
    - SQL cell: {var} + DataFrame-source reads; the output variable is the write.
      `output_variable` defaults to 'result' to match the SQL engine's default.

    - Python reads exclude the helpers the VM pre-loads (`read_athena`,
      `display`, …) — the runtime executor drops untouched injected objects too.
      Builtins stay (they never have a producing cell, so they add no edge, and
      a user cell that shadows one, e.g. `id = 5`, keeps its edge).
    - "mutated": variables the cell changes in place (see mutated_names). They
      are also listed in "writes" (and are free reads), so a consumer that only
      looks at reads/writes still sees a read-and-write.

    - "dynamic": dynamic-name constructs found (see dynamic_name_uses); [] for SQL.

    Returns {"reads": sorted[str], "writes": sorted[str], "mutated": sorted[str],
    "dynamic": sorted[str]}.
    """
    code = code or ""
    mutated: set = set()
    dynamic: list = []
    if cell_type == "sql":
        reads = sql_reads(code)
        writes = {output_variable} if (output_variable and output_variable.isidentifier()) else set()
    else:
        reads = referenced_names(code) - _preloaded_names()
        mutated = mutated_names(code) & reads
        # Modules are not data dependencies: the runtime executor drops module
        # objects from a cell's reads/writes, so the open-time estimate drops
        # `import x` / `import x as y` bindings too (always modules). `from x
        # import y` stays — y may be a function/class, decided only at runtime.
        writes = (assigned_targets(code) | mutated) - module_import_names(code)
        dynamic = dynamic_name_uses(code)
    return {"reads": sorted(reads), "writes": sorted(writes), "mutated": sorted(mutated),
            "dynamic": dynamic}


# ─── Dynamic name access ───────────────────────────────────────────────────────

# Builtins whose call makes a cell's reads/writes unknowable from the AST.
_DYNAMIC_BUILTINS = frozenset({"exec", "eval", "globals", "locals", "vars"})
# setattr/delattr only count when the target object is a module namespace.
_DYNAMIC_ATTR_BUILTINS = frozenset({"setattr", "delattr"})
_NAMESPACE_ROOTS = frozenset({"__main__", "__builtins__"})


def _rooted_at_namespace(node) -> bool:
    """True if `node` is `__main__` / `__builtins__` / `sys.modules[...]` or a
    subscript/attribute chain rooted at one of them."""
    while True:
        if isinstance(node, ast.Name):
            return node.id in _NAMESPACE_ROOTS
        if (isinstance(node, ast.Attribute) and node.attr == "modules"
                and isinstance(node.value, ast.Name) and node.value.id == "sys"):
            return True
        if isinstance(node, (ast.Subscript, ast.Attribute)):
            node = node.value
            continue
        return False


def dynamic_name_uses(code: str) -> list:
    """Dynamic-name constructs in a Python cell, as sorted display strings
    ("exec()", "globals()", "setattr()", …): calls (module scope or nested) to
    the builtins exec/eval/globals/locals/vars, and setattr/delattr whose first
    argument is rooted at sys.modules, __main__ or __builtins__. Attribute calls
    such as `df.eval(...)` do not count. SyntaxError → []."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    found: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in _DYNAMIC_BUILTINS:
            found.add(f"{name}()")
        elif name in _DYNAMIC_ATTR_BUILTINS and node.args and _rooted_at_namespace(node.args[0]):
            found.add(f"{name}()")
    return sorted(found)


# ─── Default SQL output variable (port of web _deriveSqlVarName) ──────────────

# Same patterns, same order as web/src/hooks/useNotebookCells.js _deriveSqlVarName.
# re.ASCII keeps \w identical to JavaScript's ASCII-only \w.
_SQL_VAR_PATTERNS = (
    re.compile(r"""\bFROM\s+dynamodb\."?([a-zA-Z_][\w\-]*)"?""", re.IGNORECASE | re.ASCII),
    re.compile(r"""\bFROM\s+'/tmp/([^']+)'""", re.IGNORECASE | re.ASCII),
    re.compile(r"""\bFROM\s+read_(?:csv|json|parquet)\('[^']*/([^'/]+)'\)""", re.IGNORECASE | re.ASCII),
    re.compile(r"""\bFROM\s+[a-zA-Z_]\w*\.([a-zA-Z_]\w*)""", re.IGNORECASE | re.ASCII),
)


def derive_sql_output_variable(sql: str) -> str:
    """The variable a SQL cell writes when it has no explicit outputVariable:
    the external source's table/file name (dynamodb."t", '/tmp/f.csv',
    read_csv('…/f.csv'), db.table), cleaned to an identifier; otherwise
    'result' (in-memory DataFrame queries must not overwrite their source)."""
    if not sql or not sql.strip():
        return "result"
    for pat in _SQL_VAR_PATTERNS:
        m = pat.search(sql)
        if m:
            raw = m.group(1) or "result"
            # \Z (not $): JavaScript's $ never matches before a trailing newline.
            cleaned = re.sub(r"\.\w+\Z", "", raw, count=1, flags=re.ASCII)
            cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", cleaned)
            cleaned = re.sub(r"\A_+|_+\Z", "", cleaned)
            if cleaned and re.match(r"[a-zA-Z_]", cleaned):
                return cleaned
            break
    return "result"


_PRELOADED: set | None = None


def _preloaded_names() -> set:
    """The public helper names the VM injects into every namespace
    (`display` and the read_* / plot_* / … functions of app/notebook/helpers).
    Helper names are read from the helper modules' source with `ast` (top-level
    public def/class), so this stays stdlib-only for the proxy. Cached."""
    global _PRELOADED
    if _PRELOADED is None:
        names = {"display"}
        helpers_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "helpers")
        try:
            files = sorted(f for f in os.listdir(helpers_dir) if f.endswith(".py"))
        except OSError:
            files = []
        for f in files:
            try:
                with open(os.path.join(helpers_dir, f), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError, ValueError):
                continue
            for node in tree.body:
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and not node.name.startswith("_")):
                    names.add(node.name)
        _PRELOADED = names
    return _PRELOADED


def module_import_names(code: str) -> set:
    """Names bound by plain `import x` / `import x.y` / `import x as y` statements
    anywhere in the cell. These always bind a module object."""
    names: set = set()
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names
