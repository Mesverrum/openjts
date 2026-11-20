# Final Telegraf step for every gNMI metric (tagpass jts_pipeline = "*").
#
# One output series per exported field, named junos_<measurement>_<field>.
# The OpenTelemetry output reads the single field key: "counter" becomes a
# cumulative monotonic sum (Grafana Cloud appends _total), "gauge" a gauge.
# Non-numeric strings become a gauge of 1 with the text in a "value" label.
#
# SPEC is generated from design_profile/ by scripts/generate.py and prepended
# to this file. Measurements missing from SPEC are dropped.

DROP_TAGS = ["jts_pipeline", "path", "source", "host"]
DESCRIPTION_TAG = "ifdesc/description"
MAX_VALUE_LEN = 128
# Optic dashboards key on the port (0/0/3 or 0/0/3:1). Transceiver components
# get port_id from the profile regex; these get it from if_name (et-0/0/3).
PORT_MEASUREMENTS = ["COMPONENTS", "INTERFACES_ETH"]

def clean_field(raw):
    parts = raw.rstrip("/").split("/")
    last = parts[len(parts) - 1]
    segs = last.split(":")
    last = segs[len(segs) - 1].lstrip("@")
    return last.replace("-", "_").replace(".", "_").lower()

def clean_tag(raw):
    if "/" not in raw:
        return raw.replace("-", "_").replace(".", "_").lower()
    parts = [p for p in raw.split("/") if p]
    cleaned = []
    for p in parts:
        segs = p.split(":")
        cleaned.append(segs[len(segs) - 1].lstrip("@"))
    if len(cleaned) >= 2:
        name = cleaned[len(cleaned) - 2] + "_" + cleaned[len(cleaned) - 1]
    else:
        name = cleaned[0]
    return name.replace("-", "_").replace(".", "_").lower()

def to_number(s):
    s = s.strip()
    if s == "":
        return None
    body = s
    if body[0] in "+-":
        body = body[1:]
    mant = body
    for sep in ["e", "E"]:
        if sep in body:
            pieces = body.split(sep)
            if len(pieces) != 2:
                return None
            mant = pieces[0]
            exp = pieces[1]
            if exp != "" and exp[0] in "+-":
                exp = exp[1:]
            if exp == "" or not exp.isdigit():
                return None
    mparts = mant.split(".")
    if len(mparts) > 2:
        return None
    digits = "".join(mparts)
    if digits == "" or not digits.isdigit():
        return None
    return float(s)

def apply(metric):
    spec = SPEC.get(metric.name)
    if spec == None:
        return None
    keep = spec["keep"]
    counters = spec["counters"]

    tags = {}
    for k, v in metric.tags.items():
        if k in DROP_TAGS:
            continue
        if k.endswith(DESCRIPTION_TAG):
            tags["if_description"] = v
            continue
        tags[clean_tag(k)] = v
    if metric.name in PORT_MEASUREMENTS and "port_id" not in tags and "-" in tags.get("if_name", ""):
        tags["port_id"] = tags["if_name"].split("-", 1)[1]

    mname = metric.name.lower()
    if mname.startswith("junos_"):
        mname = mname[len("junos_"):]
    base = "junos_" + mname + "_"
    out = []
    for f, v in metric.fields.items():
        name = clean_field(f)
        if name not in keep:
            continue
        key = "gauge"
        extra = None
        if type(v) == "string":
            n = to_number(v)
            if n == None:
                extra = v[:MAX_VALUE_LEN]
                v = 1
            else:
                v = n
        elif type(v) == "bool":
            v = 1 if v else 0
        if extra == None and name in counters:
            key = "counter"
        m = Metric(base + name)
        for tk, tv in tags.items():
            m.tags[tk] = tv
        if extra != None:
            m.tags["value"] = extra
        m.fields[key] = v
        m.time = metric.time
        out.append(m)
    return out
