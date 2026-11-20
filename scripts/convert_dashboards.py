#!/usr/bin/env python3
"""Convert the upstream OpenJTS InfluxQL dashboards to PromQL dashboards.

Reads design_profile/*/dashboard-*.json (kept unmodified from upstream) and
writes dashboards/openjts-<name>.json for the junos_* series that the
Telegraf pipeline exports (see scripts/profiles.py and finalize.star).

Layout, units, thresholds and field overrides are kept. Each InfluxQL target
becomes one PromQL target per selected field. Queries that cannot be expressed
from the tags and fields alone (jtso ALARMING output, NETCONF chassis alarms,
Kapacitor power aggregates, device inventory) use the rewrites in OVERRIDES.

  python3 scripts/convert_dashboards.py            # write dashboards/
  python3 scripts/convert_dashboards.py --report   # also print every translation
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profiles as P  # noqa: E402
import promql as Q  # noqa: E402

OUT_DIR = P.REPO / "dashboards"
DS = {"type": "prometheus", "uid": "${datasource}"}
UID_PREFIX = "openjts-"

# Fields jtso stored as strings. finalize.star exports these as 1 with the text in a "value" label.
STRING_FIELDS = {
    "session_state", "session_description", "policer_state", "optic_model", "optic_vendor",
    "optic_vendor_part", "optic_serial_no", "optic_func_type", "optic_fault_condition", "oper_status",
    "admin_status", "description", "cmerror_desc", "cmerror_name", "state", "transport_type",
    "module_state", "fault_condition", "vendor", "vendor_part", "serial_no", "form_factor",
    "connector_type", "ethernet_pmd", "status", "type", "mfg_name", "part_no", "serial_number",
}

# Influx tag -> Prometheus label. LINKNAME and HAS_CHANNEL depend on the measurement (see map_tag).
TAG_MAP = {
    "SHORTNAME": "device", "device": "device", "_component_id": "component_id",
    "_subcomponent_id": "sub_component_id", "DESC": "if_description",
}
# Tags that jtso enriched from its own inventory. They live on junos_device_info here.
DEVICE_INFO_TAGS = {"MODEL": "model", "VERSION": "version", "FAMILY": "family"}
PORT_KEYED = {"COMPONENTS", "INTERFACES_ETH"}
INTERFACE_KEYED = {"INTERFACES", "SUBINTERFACES", "JUNOS_QMON"}

SERIES_AGG = {"mean": "avg", "last": "max", "first": "max", "max": "max", "min": "min", "sum": "sum",
              "count": "sum", "median": "avg", "mode": "max", "spread": "max", "stddev": "avg"}
OVER_TIME = {"mean": "avg_over_time", "last": "last_over_time", "first": "last_over_time",
             "max": "max_over_time", "min": "min_over_time", "sum": "sum_over_time",
             "count": "count_over_time", "median": "avg_over_time", "stddev": "stddev_over_time"}
INSTANT_PANELS = {"table"}
REDUCING_PANELS = {"stat", "gauge", "bargauge", "piechart", "table"}


class Untranslatable(Exception):
    pass


# --------------------------------------------------------------------------- InfluxQL scanning

def scan_top(text: str):
    """Yield (index, char, depth) outside quotes and /regex/ literals."""
    depth, i, n = 0, 0, len(text)
    prev_op = False
    while i < n:
        ch = text[i]
        if ch in "'\"":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            prev_op = False
            continue
        if ch == "/" and prev_op:
            j = i + 1
            while j < n and text[j] != "/":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            prev_op = False
            continue
        if ch == "~" and i > 0 and text[i - 1] in "=!":
            prev_op = True
            i += 1
            continue
        if not ch.isspace():
            prev_op = False
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        yield i, ch, depth
        i += 1


def find_keywords(text: str, words: list[str]) -> dict[str, int]:
    low = text.lower()
    found: dict[str, int] = {}
    for i, ch, depth in scan_top(text):
        if depth != 0 or (i > 0 and (low[i - 1].isalnum() or low[i - 1] in "_\"")):
            continue
        for w in words:
            if w not in found and low.startswith(w, i):
                end = i + len(w)
                if end >= len(low) or not (low[end].isalnum() or low[end] == "_"):
                    found[w] = i
    return found


def split_top(text: str, sep: str = ",") -> list[str]:
    parts, last = [], 0
    for i, ch, depth in scan_top(text):
        if ch == sep and depth == 0:
            parts.append(text[last:i])
            last = i + 1
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]


@dataclass
class Statement:
    select: list[tuple[str, str | None]]
    source: str | "Statement"
    where: str
    group_tags: list[str]
    group_time: bool
    raw: str


def parse_statement(q: str) -> Statement:
    q = re.sub(r"\s+", " ", q).strip().rstrip(";")
    kw = find_keywords(q, ["select", "from", "where", "group by", "fill", "order by", "limit", "slimit", "tz"])
    if kw.get("select") != 0 or "from" not in kw:
        raise Untranslatable(f"not a SELECT: {q[:80]}")
    ends = sorted(v for v in kw.values())

    def section(name: str) -> str:
        if name not in kw:
            return ""
        start = kw[name] + len(name)
        nxt = [e for e in ends if e > kw[name]]
        return q[start: nxt[0] if nxt else len(q)].strip()

    select = []
    for item in split_top(section("select")):
        m = re.match(r'(?is)^(.*?)\s+as\s+(?:"([^"]+)"|([\w.\-]+))$', item)
        select.append((m.group(1).strip(), m.group(2) or m.group(3)) if m else (item, None))
    src = section("from")
    if src.startswith("("):
        source: str | Statement = parse_statement(src[1:-1])
    else:
        source = re.split(r"\.", src.replace('"', ""))[-1].strip()
    tags, has_time = [], False
    for g in split_top(section("group by")):
        if g.lower().startswith("time("):
            has_time = True
        elif g.strip() != "*":
            tags.append(g.strip().strip('"').removesuffix("::tag").strip('"'))
    return Statement(select, source, section("where"), tags, has_time, q)


# --------------------------------------------------------------------------- WHERE clause

def split_bool(text: str) -> tuple[str, list[str]]:
    """Top-level AND/OR split. Returns (operator, parts)."""
    text = text.strip()
    while text.startswith("(") and _closing(text) == len(text) - 1:
        text = text[1:-1].strip()
    low = text.lower()
    for op in ("or", "and"):
        parts, last = [], 0
        for i, ch, depth in scan_top(text):
            end = i + len(op)
            if (depth == 0 and low.startswith(op, i) and i > 0 and low[i - 1] in " )"
                    and end < len(low) and low[end] in " ("):
                parts.append(text[last:i])
                last = end
        if parts:
            parts.append(text[last:])
            return op.strip(), [p.strip() for p in parts]
    return "leaf", [text]


def _closing(text: str) -> int:
    for i, ch, depth in scan_top(text):
        if ch == ")" and depth == 0:
            return i
    return -1


LEAF = re.compile(r'(?s)^"?([\w.\-:/]+?)"?(?:::(?:tag|field))?\s*(=~|!~|!=|<>|>=|<=|=|>|<)\s*(.+)$')


@dataclass
class Cond:
    key: str
    op: str
    value: str          # regex body, string literal, or number text
    kind: str           # regex | string | number


def parse_conditions(text: str) -> list[Cond | list[Cond]]:
    """Flatten AND groups. An OR group comes back as a list of Conds."""
    if not text.strip():
        return []
    op, parts = split_bool(text)
    if op == "leaf":
        c = parse_leaf(parts[0])
        return [c] if c else []
    if op == "and":
        out: list = []
        for p in parts:
            out.extend(parse_conditions(p))
        return out
    alts = []
    for p in parts:
        sub = parse_conditions(p)
        if len(sub) != 1 or isinstance(sub[0], list):
            raise Untranslatable(f"nested OR: {text}")
        alts.append(sub[0])
    return [alts]


def parse_leaf(text: str) -> Cond | None:
    text = text.strip()
    if text.lower() in ("$timefilter", "true"):
        return None
    m = LEAF.match(text)
    if not m:
        raise Untranslatable(f"condition {text!r}")
    key, op, val = m.group(1), m.group(2), m.group(3).strip()
    if key.lower() == "time":
        return None
    if op == "<>":
        op = "!="
    if val.startswith("/") and val.endswith("/"):
        return Cond(key, op, val[1:-1], "regex")
    if val.startswith("'") and val.endswith("'"):
        return Cond(key, op, val[1:-1], "string")
    if re.fullmatch(r"-?[\d.]+", val):
        return Cond(key, op, val, "number")
    if val.startswith("$"):
        return Cond(key, op, val, "string")
    raise Untranslatable(f"condition value {text!r}")


VAR_ONLY = re.compile(r"^(?:\^)?(?:\.\*)?(\$\{?\w+(?::\w+)?\}?)(?:\.\*)?(?:\$)?$")


def prom_regex(body: str) -> str:
    start = body.startswith("^")
    end = body.endswith("$") and not body.endswith("\\$") and not re.search(r"\$\{?\w+(?::\w+)?\}?$", body)
    # A trailing $ right after a variable reference is an anchor, not part of the variable.
    if not end and re.search(r"(\$\{?\w+(?::\w+)?\}?)\$$", body):
        end = True
        body = body[:-1]
    core = body[1 if start else 0: len(body) - (1 if end and body.endswith("$") else 0)]
    if (not start or not end) and _top_level_alternation(core):
        core = f"(?:{core})"
    if not start and not core.startswith(".*"):
        core = ".*" + core
    if not end and not core.endswith(".*"):
        core = core + ".*"
    if core in (".*.*", ""):
        core = ".*"
    return core


def _top_level_alternation(rx: str) -> bool:
    depth, i = 0, 0
    while i < len(rx):
        ch = rx[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            return True
        i += 1
    return False


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class Selection:
    matchers: list[str] = field(default_factory=list)
    field_conds: list[Cond] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)     # "and on(device) ..." suffixes
    group_joins: list[str] = field(default_factory=list)


def map_tag_key(key: str, measurement: str, value: str = "") -> str:
    key = key.strip('"')
    if key in TAG_MAP:
        return TAG_MAP[key]
    if key == "LINKNAME":
        if measurement in PORT_KEYED:
            return "port_id"
        return "if_name"
    return P.clean_name(key) if "/" in key else key.replace("-", "_").lower()


def cond_matcher(c: Cond, measurement: str, sel: Selection) -> None:
    key = c.key.strip('"')
    if key == "HAS_CHANNEL":
        yes = (c.op in ("=", "=~") and "yes" in c.value) or (c.op in ("!=", "!~") and "no" in c.value)
        sel.matchers.append('port_id=~".*:.*"' if yes else 'port_id!~".*:.*"')
        return
    if key in DEVICE_INFO_TAGS:
        if c.value == "" and c.op in ("!=", "!~"):
            return
        label = DEVICE_INFO_TAGS[key]
        sel.joins.append(f"and on(device) junos_device_info{{{label}{_op(c)}\"{esc(_val(c))}\"}}")
        return
    if key == "OPTIC_DESC":
        sel.joins.append(f'and on(device, port_id) junos_components_optic_model{{value=~"{esc(_val(c) if c.kind == "regex" else ".*" + c.value + ".*")}"}}')
        return
    if key.startswith("MPLS_"):
        return
    label = map_tag_key(key, measurement)
    if Q.is_known(measurement, key) or Q.is_known(measurement, key.removesuffix("_rate")):
        sel.field_conds.append(c)
        return
    if key == "LINKNAME" and measurement == "INTERFACES" and c.kind == "regex" and re.search(r"\$\{?(port|cage)\b", c.value):
        m = VAR_ONLY.match(c.value)
        if m:
            sel.matchers.append(f'if_name=~"[a-z]+-{m.group(1)}"')
            return
    if c.kind == "regex" and key == "LINKNAME":
        m = VAR_ONLY.match(c.value)
        if m:
            sel.matchers.append(f'{label}=~"{m.group(1)}"')
            return
    if c.kind == "regex" and label == "ifl_name":
        m = VAR_ONLY.match(c.value)
        if m:
            # Interface variables hold physical names; SR-MPLS stats are per logical unit.
            sel.matchers.append(f'ifl_name=~"{m.group(1)}(\\\\..+)?"')
            return
    sel.matchers.append(f'{label}{_op(c)}"{esc(_val(c))}"')


def _op(c: Cond) -> str:
    if c.kind == "regex":
        return c.op
    if "$" in c.value:
        return "=~" if c.op == "=" else "!~"
    return c.op


def _val(c: Cond) -> str:
    if c.kind == "regex":
        # /^$rtr$/, /$rtr/ and /^.*${port:regex}.*$/ all mean "the selected value" once
        # variables hold exact label values.
        m = VAR_ONLY.match(c.value)
        if m:
            return m.group(1)
        return prom_regex(c.value)
    return c.value


def build_selection(where: str, measurement: str) -> Selection:
    sel = Selection()
    for c in parse_conditions(where):
        if isinstance(c, list):
            keys = {map_tag_key(x.key, measurement) for x in c}
            if len(keys) != 1 or any(x.op not in ("=", "=~") for x in c):
                raise Untranslatable(f"OR across keys {[x.key for x in c]}")
            alts = [_val(x) if x.kind == "regex" else re.escape(x.value) for x in c]
            sel.matchers.append(f'{keys.pop()}=~"{esc("|".join(alts))}"')
        else:
            cond_matcher(c, measurement, sel)
    return sel


# --------------------------------------------------------------------------- select expressions

TOKEN = re.compile(r'\s*(?:(?P<num>\d+(?:\.\d+)?(?:e[+-]?\d+)?)|"(?P<q>[^"]+)"|(?P<id>[A-Za-z_][\w.\-]*)|(?P<op>[-+*/(),]))', re.I)


def tokenize(expr: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    expr = expr.strip()
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise Untranslatable(f"token at {expr[pos:pos + 20]!r}")
        pos = m.end()
        for kind in ("num", "q", "id", "op"):
            if m.group(kind) is not None:
                out.append((kind if kind != "q" else "id", m.group(kind)))
                break
    return out


class ExprParser:
    """Recursive descent over InfluxQL select arithmetic. leaf(name, func) renders a field."""

    def __init__(self, tokens, leaf):
        self.t, self.i, self.leaf = tokens, 0, leaf
        self.funcs: list[str] = []
        self.fields: list[str] = []

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise Untranslatable(f"expected {value}, got {tok}")
        self.i += 1
        return tok

    def parse(self) -> tuple[str, bool]:
        out = self.expr()
        if self.i != len(self.t):
            raise Untranslatable(f"trailing tokens {self.t[self.i:]}")
        return out

    # returns (promql, is_compound)
    def expr(self):
        left, comp = self.term()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            right, _ = self.term()
            left, comp = f"{left} {op} {right}", True
        return left, comp

    def term(self):
        left, comp = self.factor()
        while self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            right, rcomp = self.factor()
            if comp:
                left = f"({left})"
            if rcomp:
                right = f"({right})"
            left, comp = f"{left} {op} {right}", False
        return left, comp

    def factor(self):
        kind, val = self.peek()
        if kind == "num":
            self.take()
            return val, False
        if val == "-":
            self.take()
            inner, comp = self.factor()
            return f"-({inner})" if comp else f"-{inner}", False
        if val == "(":
            self.take()
            inner, comp = self.expr()
            self.take(")")
            return (f"({inner})" if comp else inner), False
        if kind == "id":
            self.take()
            if self.peek()[1] == "(":
                func = val.lower()
                self.take("(")
                k2, name = self.take()
                if k2 != "id":
                    raise Untranslatable(f"{func}() of {name}")
                while self.peek()[1] == ",":
                    self.take()
                    self.take()
                self.take(")")
                self.funcs.append(func)
                self.fields.append(name)
                return self.leaf(name, func), False
            self.fields.append(val)
            return self.leaf(val, None), False
        raise Untranslatable(f"unexpected {val!r}")


# --------------------------------------------------------------------------- translation

@dataclass
class Target:
    expr: str
    legend: str = ""
    instant: bool = False
    fmt: str = "time_series"
    column: str = ""        # Influx value column name, for table renames
    group: list[str] = field(default_factory=list)
    group_influx: list[str] = field(default_factory=list)
    string: bool = False


@dataclass
class Ctx:
    dashboard: str
    panel_type: str
    title: str
    warnings: list[str]


BARE_SELECTOR = re.compile(r'^[a-zA-Z_:][\w:]*(\{(?:[^{}"]|"(?:[^"\\]|\\.)*")*\})?$')


def over(fn: str, expr: str, window: str = "$__range") -> str:
    """fn(expr[window]); compound expressions need subquery syntax."""
    return f"{fn}({expr}[{window}])" if BARE_SELECTOR.match(expr) else f"{fn}({expr}[{window}:])"


def leaf_builder(stmt: Statement, measurement: str, sel: Selection, group: list[str], instant: bool, ctx: Ctx, flags: dict):
    def leaf(name: str, func: str | None) -> str:
        cname = P.clean_name(name.removesuffix("_rate"))
        base_known = Q.is_known(measurement, name) or Q.is_known(measurement, name.removesuffix("_rate"))
        if not base_known:
            ctx.warnings.append(f"{measurement}.{name}: no profile exports this field")
        if cname in STRING_FIELDS and not name.endswith("_rate"):
            flags["string"] = True
        matchers = list(sel.matchers)
        for c in sel.field_conds:
            if P.clean_name(c.key.removesuffix("_rate")) == cname and c.kind != "number" and cname in STRING_FIELDS:
                matchers.append(f'value{_op(c)}"{esc(_val(c))}"')
        expr, is_rate = Q.field_ref(measurement, name, matchers)
        for c in sel.field_conds:
            if P.clean_name(c.key.removesuffix("_rate")) == cname:
                if c.kind == "number":
                    op = "==" if c.op == "=" else c.op
                    expr = f"({expr} {op} {c.value})"
            else:
                other, _ = Q.field_ref(measurement, c.key.strip('"'), matchers)
                fname = P.clean_name(c.key.removesuffix("_rate"))
                if c.kind == "number":
                    expr = f"({expr} and ({other} {c.op if c.op != '=' else '=='} {c.value}))"
                elif fname in STRING_FIELDS:
                    vm = _val(c) if c.kind == "regex" else c.value
                    otherm = other[:-1] + f', value{c.op if c.kind == "regex" else ("=" if c.op == "=" else "!=")}"{esc(vm)}"}}' if other.endswith("}") else f'{other}{{value=~"{esc(vm)}"}}'
                    expr = f"({expr} and ignoring(value) {otherm})"
        for j in sel.joins:
            expr = f"({expr} {j})"
        info_labels = [g for g in group if g in DEVICE_INFO_TAGS.values()]
        if info_labels:
            expr = f"({expr} * on(device) group_left({', '.join(info_labels)}) junos_device_info)"
        if func is None:
            if instant and not flags.get("string"):
                return over("last_over_time", expr) if not is_rate else expr
            return expr
        if func in ("non_negative_derivative", "derivative"):
            raw, _ = Q.field_ref(measurement, name, matchers)
            return f"rate({raw}[$__rate_interval])" if Q.is_counter(measurement, name) else f"deriv({raw}[$__rate_interval])"
        if func == "difference":
            return f"delta({expr}[$__interval])"
        if func not in SERIES_AGG:
            raise Untranslatable(f"function {func}()")
        if instant:
            if func == "mean" and is_rate:
                raw, _ = Q.field_ref(measurement, name.removesuffix("_rate"), matchers)
                inner = f"rate({raw}[$__range])"
            elif func in ("last", "first") and flags.get("string"):
                inner = expr
            else:
                inner = over(OVER_TIME[func], expr)
        elif func == "count":
            inner = over("count_over_time", expr, "$__interval")
        else:
            inner = expr
        by = ", ".join(group)
        if flags.get("string"):
            by = ", ".join(group + ["value"])
        return f"{SERIES_AGG[func]} by ({by}) ({inner})" if group or flags.get("string") else f"{SERIES_AGG[func]}({inner})"
    return leaf


def translate(query: str, ctx: Ctx, alias: str | None) -> list[Target]:
    stmt = parse_statement(query)
    if isinstance(stmt.source, Statement):
        return translate_subquery(stmt, ctx, alias)
    measurement = stmt.source
    if measurement in ("ALARMING", "JUNOS_ALARMS"):
        raise Untranslatable(f"{measurement} needs an override")
    sel = build_selection(stmt.where, measurement)
    group = [map_tag_key(g, measurement) if g not in DEVICE_INFO_TAGS else DEVICE_INFO_TAGS[g] for g in stmt.group_tags]
    instant = ctx.panel_type in INSTANT_PANELS or (ctx.panel_type in REDUCING_PANELS and not stmt.group_time)
    targets = []
    used_cols: dict[str, int] = {}
    for item, as_name in stmt.select:
        flags: dict = {}
        parser = ExprParser(tokenize(item), leaf_builder(stmt, measurement, sel, group, instant, ctx, flags))
        expr, _ = parser.parse()
        if instant and not parser.funcs and len(parser.fields) == 1 and expr.startswith("last_over_time") and ctx.panel_type not in INSTANT_PANELS:
            pass
        col = as_name or (parser.funcs[0] if parser.funcs and len(parser.fields) == 1 and item.strip().lower().startswith(parser.funcs[0]) else parser.fields[0] if len(parser.fields) == 1 and not parser.funcs else (parser.funcs[0] if parser.funcs else "value"))
        if col in used_cols:
            used_cols[col] += 1
            col = f"{col}_{used_cols[col]}"
        else:
            used_cols[col] = 0
        legend = influx_alias(alias, col, stmt.group_tags, measurement) if alias else default_legend(stmt, group, col, len(stmt.select), flags.get("string"))
        targets.append(Target(expr=expr, legend=legend, instant=instant,
                              fmt="table" if ctx.panel_type == "table" else "time_series",
                              column=col, group=group, group_influx=list(stmt.group_tags), string=bool(flags.get("string"))))
    return targets


def translate_subquery(stmt: Statement, ctx: Ctx, alias: str | None) -> list[Target]:
    inner = stmt.source
    assert isinstance(inner, Statement)
    measurement = inner.source if isinstance(inner.source, str) else None
    if measurement is None:
        raise Untranslatable("nested subqueries")
    inner_ctx = Ctx(ctx.dashboard, "timeseries", ctx.title, ctx.warnings)
    instant_outer = ctx.panel_type in INSTANT_PANELS or (ctx.panel_type in REDUCING_PANELS and not stmt.group_time)
    if instant_outer:
        inner_ctx.panel_type = "stat" if not inner.group_time else "timeseries"
    inner_targets = translate(inner.raw, inner_ctx, None)
    named = {}
    for (item, as_name), t in zip(inner.select, inner_targets):
        named[as_name or t.column] = t.expr
    group = [map_tag_key(g, measurement) if g not in DEVICE_INFO_TAGS else DEVICE_INFO_TAGS[g] for g in stmt.group_tags]
    targets = []
    for item, as_name in stmt.select:
        funcs: list[str] = []

        def leaf(name: str, func: str | None) -> str:
            if name not in named:
                raise Untranslatable(f"outer reference {name}")
            inner_expr = named[name]
            if func is None:
                return inner_expr
            funcs.append(func)
            if func not in SERIES_AGG:
                raise Untranslatable(f"outer function {func}()")
            agg = "count" if func == "count" else SERIES_AGG[func]
            return f"{agg} by ({', '.join(group)}) ({inner_expr})" if group else f"{agg}({inner_expr})"

        parser = ExprParser(tokenize(item), leaf)
        expr, _ = parser.parse()
        col = as_name or (funcs[0] if funcs else "value")
        legend = influx_alias(alias, col, stmt.group_tags, measurement) if alias else default_legend(stmt, group, col, len(stmt.select), False)
        targets.append(Target(expr=expr, legend=legend, instant=instant_outer,
                              fmt="table" if ctx.panel_type == "table" else "time_series",
                              column=col, group=group, group_influx=list(stmt.group_tags)))
    return targets


def influx_alias(alias: str, col: str, group_tags: list[str], measurement: str) -> str:
    def tag(m):
        name = m.group(1) or m.group(2)
        mapped = DEVICE_INFO_TAGS.get(name) or map_tag_key(name, measurement)
        return "{{" + mapped + "}}"
    out = re.sub(r"\$tag_([\w]+)|\[\[tag_([\w]+)\]\]", tag, alias)
    out = out.replace("$col", col).replace("[[col]]", col).replace("$m", measurement).replace("[[m]]", measurement)
    return out


def default_legend(stmt: Statement, group: list[str], col: str, n_items: int, string: bool) -> str:
    if string:
        return "{{value}}"
    parts = ["{{" + g + "}}" for g in group]
    if n_items > 1 or not parts:
        parts.insert(0, col)
    return " ".join(parts)


# --------------------------------------------------------------------------- builder-mode targets

def render_builder(t: dict) -> str:
    sel = []
    for chain in t.get("select") or []:
        expr = None
        alias = None
        for part in chain:
            typ, params = part.get("type"), part.get("params") or []
            if typ == "field":
                expr = f'"{params[0]}"'
            elif typ == "alias":
                alias = params[0]
            elif typ == "math":
                expr = f"{expr} {params[0]}"
            elif typ in SERIES_AGG or typ in ("non_negative_derivative", "derivative", "difference"):
                expr = f"{typ}({expr})"
            else:
                raise Untranslatable(f"builder function {typ}")
        sel.append(expr + (f' AS "{alias}"' if alias else ""))
    where = []
    for i, tag in enumerate(t.get("tags") or []):
        key = tag["key"].removesuffix("::tag")
        clause = f'"{key}" {tag.get("operator", "=")} '
        val = tag["value"]
        clause += val if val.startswith("/") else f"'{val}'"
        if i:
            clause = f"{tag.get('condition', 'AND')} {clause}"
        where.append(clause)
    q = f'SELECT {", ".join(sel)} FROM "{t["measurement"]}" WHERE ({" ".join(where)}) AND $timeFilter' if where \
        else f'SELECT {", ".join(sel)} FROM "{t["measurement"]}" WHERE $timeFilter'
    groups = []
    for g in t.get("groupBy") or []:
        if g["type"] == "time":
            groups.append(f"time({g['params'][0]})")
        elif g["type"] == "tag":
            groups.append(f'"{g["params"][0].removesuffix("::tag")}"')
    if groups:
        q += " GROUP BY " + ", ".join(groups)
    return q


def target_query(t: dict) -> str | None:
    if t.get("query") and t.get("rawQuery") is not False:
        return t["query"]
    if t.get("measurement") and t.get("select"):
        return render_builder(t)
    return t.get("query")


# --------------------------------------------------------------------------- variables

def show_tag_values(q: str, primary: str) -> str:
    m = re.match(r'(?is)^\s*show\s+tag\s+values\s*(?:from\s+"?([\w.\-]+)"?\s*)?with\s+key\s*=\s*"?([\w\-/]+)"?\s*(?:where\s+(.+))?$', q.strip())
    if not m:
        raise Untranslatable(f"variable query {q!r}")
    meas, key, where = m.group(1) or primary, m.group(2), m.group(3) or ""
    if key in DEVICE_INFO_TAGS or (key == "SHORTNAME" and re.search(r'"?FAMILY"?\s*=', where)):
        sel = build_selection(where, meas)
        info = [x for x in (j.split("junos_device_info", 1)[1] for j in sel.joins if "junos_device_info" in j)]
        matchers = ", ".join(x.strip("{}") for x in info)
        label = DEVICE_INFO_TAGS.get(key, "device")
        return f"label_values(junos_device_info{{{matchers}}}, {label})"
    sel = build_selection(where, meas)
    label = map_tag_key(key, meas)
    matchers = [f'__name__=~"{P.measurement_prefix(meas)}.+"'] + sel.matchers
    return f"label_values({{{', '.join(matchers)}}}, {label})"


def variable_label(q: str, primary: str) -> str | None:
    m = re.search(r'(?is)(?:from\s+"?([\w.\-]+)"?\s*)?with\s+key\s*=\s*"?([\w\-/]+)"?', q or "")
    return map_tag_key(m.group(2), m.group(1) or primary) if m else None


def convert_variable(v: dict, primary: str, warnings: list[str]) -> dict | None:
    if v.get("type") == "datasource":
        return None
    v = copy.deepcopy(v)
    if v.get("type") == "query":
        q = v.get("query")
        if isinstance(q, dict):
            q = q.get("query")
        try:
            pq = VAR_OVERRIDES.get(v["name"], {}).get(primary) or show_tag_values(q, primary)
        except Untranslatable as exc:
            warnings.append(f"variable {v['name']}: {exc}")
            pq = f'label_values({{__name__=~"{P.measurement_prefix(primary)}.+"}}, device)'
        if variable_label(q, primary) == "port_id" and "-(" in (v.get("regex") or ""):
            v["regex"] = ""  # the old regex cut 0/0/3 out of LINKNAME; port_id already is that
        v["datasource"] = DS
        v["query"] = {"qryType": 1, "query": pq, "refId": "PrometheusVariableQueryEditor-VariableQuery"}
        v["definition"] = pq
        v["refresh"] = 2
        v.pop("tagsQuery", None)
        v.pop("tagValuesQuery", None)
        v["current"] = {}
        v["options"] = []
    return v


VAR_OVERRIDES: dict[str, dict[str, str]] = {}


# --------------------------------------------------------------------------- overrides

def alarm_targets(name_regex: str, labels: list[str], mode: str) -> list[Target]:
    """jtso ALARMING rows rebuilt from monitoring_list probes. mode=count|detail."""
    rx = re.compile(f"^(?:{name_regex})$") if name_regex else None
    out = []
    for probe in sorted(P.probes(), key=lambda p: p["name"]):
        if rx and not rx.match(probe["name"]):
            continue
        try:
            cond = Q.probe_expr(probe, ['device=~"$rtr"'], window="$__rate_interval")
        except P.ProfileError:
            continue
        name = probe["name"]
        if mode == "count":
            by = ", ".join(labels)
            expr = f'label_replace(count by ({by}) ({cond}), "alarm_type", "{name}", "", "")' if by \
                else f'label_replace(count({cond}) or vector(0), "alarm_type", "{name}", "", "")'
        else:
            expr = f'label_replace({cond}, "alarm_type", "{name}", "", "")'
        if any(t.expr == expr for t in out):
            continue
        out.append(Target(expr=expr, legend="{{alarm_type}}", instant=True, fmt="table", column="count" if mode == "count" else "exception"))
    if not out:
        raise Untranslatable(f"no probes match {name_regex!r}")
    return [Target(expr=" or ".join(t.expr for t in out), legend="{{alarm_type}}", instant=True, fmt="table",
                   column=out[0].column)]


def alarming(query: str) -> list[Target]:
    stmt = parse_statement(query)
    names = ""
    for c in parse_conditions(stmt.where):
        if isinstance(c, Cond) and c.key.strip('"') == "ALARM_TYPE":
            names = c.value if c.kind == "regex" else re.escape(c.value)
    labels = [map_tag_key(g, "INTERFACES") for g in stmt.group_tags if g != "ALARM_TYPE" and g not in ("queue_name",)]
    labels = ["if_name" if l in ("if_name", "port_id") else l for l in labels]
    mode = "count" if re.search(r"(?i)count\(", stmt.raw) else "detail"
    return alarm_targets(names.strip("^$"), sorted(set(labels)), mode)


DEVICE_INFO_TABLE = [Target(expr='max by (device, family, model, version, site) (junos_device_info{device=~"$rtr"})',
                            instant=True, fmt="table", column="info")]
NETCONF_NOTE = ("Chassis alarms were collected over NETCONF by jtso. This fork does not run a NETCONF "
                "collector. Add the gNMI path /system/alarms to a profile, or use the Junos syslog "
                "feed in Loki, to bring them back.")

# Device power: the three chassis power fields summed per device (any of them may be missing).
POWER = 'sum by (device) ({__name__=~"junos_components_power_(system|zone_0|zone_1)_usage", device=~"$SHORTNAME"})'
POWER_ALL = 'sum by (device) ({__name__=~"junos_components_power_(system|zone_0|zone_1)_usage"})'
TRAFFIC_GBPS = 'sum by (device) (rate(junos_interfaces_in_octets_total{device=~"$SHORTNAME", if_unit=""}[$__rate_interval])) * 8 / 1e9'
# SR-MPLS share of an interface: per-unit SR counters rolled up to the physical port.
SR_RATIO = ('sum by (if_name) (label_replace(rate(junos_srmpls_if_stats_sr_{d}_octets_total{{device=~"$rtr", '
            'ifl_name=~"${{ifname:regex}}(\\\\..+)?"}}[$__rate_interval]), "if_name", "$1", "ifl_name", "([^.]+).*")) '
            '/ sum by (if_name) (rate(junos_interfaces_{d}_octets_total{{device=~"$rtr", if_name=~"${{ifname:regex}}", '
            'if_unit=""}}[$__rate_interval]))')

# (dashboard file, panel title) -> list[Target] | "text:<markdown>"; title "*" matches any panel.
OVERRIDES: dict[tuple[str, str], object] = {
    ("dashboard-health.json", "Router Info"): DEVICE_INFO_TABLE,
    ("dashboard-hw-npu.json", "Router Info"): DEVICE_INFO_TABLE,
    ("dashboard-srmpls-iif.json", "Router Info"): DEVICE_INFO_TABLE,
    ("dashboard-srmpls-sid.json", "Router Info"): DEVICE_INFO_TABLE,
    ("dashboard-power.json", "Chassis Info"): [Target(expr='max by (device, family, model, version, site) (junos_device_info{device=~"$SHORTNAME"})', instant=True, fmt="table", column="info")],
    ("dashboard-health.json", "Chassis Alarms"): "text:" + NETCONF_NOTE,
    ("dashboard-hw-npu.json", "Chassis Alarms"): "text:" + NETCONF_NOTE,
    ("dashboard-power.json", "Ratio Watts/Gbps - $SHORTNAME"): [
        Target(expr=f"{POWER} / ({TRAFFIC_GBPS})", legend="{{device}} W per Gbps", column="ratio")],
    ("dashboard-srmpls-iif.json", "In SR MPLS Ratio"): [Target(expr=SR_RATIO.format(d="in"), legend="{{if_name}}", instant=True, column="percent_in")],
    ("dashboard-srmpls-iif.json", "Out SR MPLS Ratio"): [Target(expr=SR_RATIO.format(d="out"), legend="{{if_name}}", instant=True, column="percent_out")],
}

TITLES = {
    "bgp": "BGP", "ddos": "DDoS Protection", "filter": "Firewall Filters", "health": "Router Health",
    "hw-npu": "Hardware KPIs (NPU)", "optic": "Optics", "zr": "ZR/ZR+ Optics", "power": "Power",
    "power_agg": "Power by Model", "srmpls-iif": "SR-MPLS per Interface", "srmpls-sid": "SR-MPLS per SID",
    "traffic": "Physical Interfaces", "unit_traffic": "Logical Interfaces", "voq": "VoQ (PTX)",
    "twamp": "TWAMP SLA",
}


def power_agg_overrides(title: str, query: str) -> list[Target] | None:
    q = query.lower()
    if "power_model" not in q and "power_agg" not in q:
        return None
    by_model = "model" in q.split("group by")[-1] if "group by" in q else False
    per_device = f"({POWER_ALL} * on(device) group_left(model) junos_device_info)"
    if "count" in q:
        expr = "count by (model) (junos_device_info)" if by_model else "count(junos_device_info)"
    elif by_model:
        expr = f"sum by (model) {per_device}"
    else:
        expr = f"sum({POWER_ALL})"
    return [Target(expr=expr, legend="{{model}}" if by_model else "total", column="value")]


def special_targets(fname: str, title: str, query: str, ctx: Ctx) -> list[Target] | None:
    if re.search(r'(?i)from\s+"?ALARMING"?', query):
        return alarming(query)
    if re.search(r'(?i)from\s+"?JUNOS_ALARMS"?', query):
        return None
    if fname == "dashboard-power_agg.json":
        return power_agg_overrides(title, query)
    if "SHORTNAME_traffic_in" in query:
        return [Target(expr=TRAFFIC_GBPS, legend="{{device}}")]
    return None


# --------------------------------------------------------------------------- panels

def walk(panels):
    for p in panels:
        yield p
        yield from walk(p.get("panels") or [])


def text_panel(p: dict, content: str) -> dict:
    keep = {k: p[k] for k in ("id", "gridPos", "title", "description") if k in p}
    return {**keep, "type": "text", "options": {"mode": "markdown", "content": content}}


def table_transforms(targets: list[Target], measurement_tags: dict[str, str]) -> list[dict]:
    rename, exclude = {}, {"Time": True, "__name__": True, "job": True, "instance": True}
    for i, t in enumerate(targets):
        ref = chr(ord("A") + i)
        rename[f"Value #{ref}" if len(targets) > 1 else "Value"] = t.column
        for g, gi in zip(t.group, t.group_influx):
            if g != gi:
                rename[g] = gi
    return [{"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": exclude, "renameByName": rename, "indexByName": {}}}]


KEEP_TRANSFORMS = {"organize", "filterFieldsByName", "convertFieldType", "reduce", "sortBy", "filterByValue", "calculateField", "configFromData"}


def convert_panel(fname: str, p: dict, report: list[str], warnings: list[str]) -> dict:
    if p.get("type") == "row":
        p = copy.deepcopy(p)
        p.pop("datasource", None)
        if p.get("panels"):
            p["panels"] = [convert_panel(fname, c, report, warnings) for c in p["panels"]]
        return p
    title = p.get("title") or ""
    override = OVERRIDES.get((fname, title))
    if isinstance(override, str) and override.startswith("text:"):
        report.append(f"  [{p.get('type')}] {title!r} -> text note")
        return text_panel(p, override[5:])
    if not p.get("targets"):
        out = copy.deepcopy(p)
        if "datasource" in out and p.get("type") != "text":
            out["datasource"] = DS
        return out
    out = copy.deepcopy(p)
    if out.get("type") == "jdbranham-diagram-panel" or out.get("type") == "yesoreyeram-boomtable-panel":
        out["type"] = "table"
        out.pop("options", None)
    ptype = out.get("type", "timeseries")
    if ptype == "graph":
        out["type"] = ptype = "timeseries"
    ctx = Ctx(fname, ptype, title, warnings)
    new_targets: list[Target] = []
    netconf = False
    for t in p["targets"]:
        if t.get("hide"):
            continue
        q = target_query(t)
        if not q:
            continue
        try:
            if isinstance(override, list):
                tlist = override
            else:
                tlist = special_targets(fname, title, q, ctx)
                if tlist is None and re.search(r'(?i)from\s+"?JUNOS_ALARMS"?', q):
                    netconf = True
                    continue
                if tlist is None:
                    tlist = translate(q, ctx, t.get("alias"))
        except Untranslatable as exc:
            warnings.append(f"{title!r}: {exc} :: {re.sub(r'\s+', ' ', q)[:160]}")
            continue
        for nt in tlist:
            report.append(f"  [{ptype}] {title!r}\n      {re.sub(r'\s+', ' ', q)[:200]}\n   => {nt.expr}")
        new_targets.extend(tlist)
        if isinstance(override, list):
            break
    if netconf and not new_targets:
        report.append(f"  [{ptype}] {title!r} -> text note (NETCONF)")
        return text_panel(p, NETCONF_NOTE)
    if not new_targets:
        return text_panel(p, f"Not converted: no PromQL equivalent for the original query. See docs/migration.md.")
    out["datasource"] = DS
    out["targets"] = []
    for i, t in enumerate(new_targets):
        tgt = {"refId": chr(ord("A") + i), "datasource": DS, "expr": t.expr, "legendFormat": t.legend or "__auto",
               "range": not t.instant, "instant": t.instant, "format": t.fmt, "editorMode": "code"}
        out["targets"].append(tgt)
    kept = [] if isinstance(override, list) else [
        x for x in p.get("transformations") or [] if x.get("id") in KEEP_TRANSFORMS]
    if ptype == "table":
        out["transformations"] = table_transforms(new_targets, {}) + kept
    else:
        out["transformations"] = kept
    if ptype == "stat" and all(t.string for t in new_targets):
        out.setdefault("options", {})["textMode"] = "name"
    return out


def primary_measurement(d: dict) -> str:
    counts: dict[str, int] = {}
    for p in walk(d.get("panels", [])):
        for t in p.get("targets") or []:
            q = target_query(t) or ""
            for m in re.findall(r'(?i)from\s+"?([A-Z_][\w]*)"?', q):
                if m not in ("ALARMING", "JUNOS_ALARMS"):
                    counts[m] = counts.get(m, 0) + 1
    return max(counts, key=counts.get) if counts else "INTERFACES"


def convert_dashboard(path: Path, uid_map: dict[str, str]) -> tuple[dict, list[str], list[str]]:
    d = json.loads(path.read_text(encoding="utf-8"))
    fname = path.name
    report, warnings = [], []
    primary = primary_measurement(d)
    out = copy.deepcopy(d)
    for k in ("__inputs", "__requires", "__elements", "id", "version", "iteration"):
        out.pop(k, None)
    out["uid"] = uid_map[d.get("uid") or path.stem]
    out["title"] = f"OpenJTS / {TITLES.get(path.stem.removeprefix('dashboard-'), d.get('title', path.stem))}"
    out["tags"] = sorted(set((d.get("tags") or []) + ["openjts", "junos", path.parent.name]))
    out["panels"] = [convert_panel(fname, p, report, warnings) for p in d.get("panels", [])]
    variables = [{"name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus",
                  "current": {}, "hide": 0, "refresh": 1, "regex": "", "options": []}]
    for v in (d.get("templating") or {}).get("list", []):
        nv = convert_variable(v, primary, warnings)
        if nv:
            variables.append(nv)
    out["templating"] = {"list": variables}
    out["annotations"] = {"list": [a for a in (d.get("annotations") or {}).get("list", []) if a.get("builtIn")]}
    for link in out.get("links") or []:
        if link.get("url"):
            for old, new in uid_map.items():
                link["url"] = link["url"].replace(f"/d/{old}", f"/d/{new}")
    text = json.dumps(out)
    for old, new in uid_map.items():
        text = text.replace(f"/d/{old}/", f"/d/{new}/")
    out = json.loads(text)
    return out, report, warnings


def dashboard_files() -> list[Path]:
    return sorted(P.PROFILE_DIR.glob("*/dashboard-*.json"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--report", action="store_true", help="print every query translation")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    files = dashboard_files()
    uid_map = {}
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        uid_map[d.get("uid") or f.stem] = UID_PREFIX + f.stem.removeprefix("dashboard-").replace("_", "-")
    args.out.mkdir(parents=True, exist_ok=True)
    total_warn = 0
    for f in files:
        dash, report, warnings = convert_dashboard(f, uid_map)
        target = args.out / f"{dash['uid']}.json"
        target.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        n = sum(len(p.get("targets") or []) for p in walk(dash["panels"]))
        print(f"{target.name:40} {n:3} targets  {len(warnings)} warnings")
        if args.report:
            print("\n".join(report))
        for w in warnings:
            print(f"    ! {w}")
        total_warn += len(warnings)
    print(f"\n{len(files)} dashboards, {total_warn} warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
