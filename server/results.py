"""Calcul et page des resultats (port prive seulement)."""
import json
import sqlite3
import statistics
import time

FORMATS = {"list": "Liste", "compact": "Compact", "stack": "Empilé", "cols": "Deux colonnes"}
MISSIONS = {"nav": "Se retrouver", "diff": "Repérer un écart", "empty": "Repérer un vide"}
RATINGS = {"retrouver": "Se retrouver", "reperer": "Repérer", "suivre": "Suivre", "confort": "Confort"}
METIER = {"courtier", "adjoint"}


def _median(xs):
    return round(statistics.median(xs), 1) if xs else None


def _load(con: sqlite3.Connection):
    people: dict[str, dict] = {}

    def person(vid):
        return people.setdefault(vid, {"visitor": vid, "first_seen": None, "sessions": 0, "active": 0, "fmt_all": {}, "fmt_free": {},
                                       "manual": {}, "missions": {}, "vote": None, "revisions": 0, "ip": set(), "ua": {}, "voted_at": None})

    for sid, vid, first, last, total, active, ua, iph in con.execute(
            "SELECT session_id, visitor_id, first_seen, last_seen, active_total, active, ua, ip_hash FROM sessions ORDER BY first_seen"):
        p = person(vid)
        p["first_seen"] = first if p["first_seen"] is None else min(p["first_seen"], first)
        p["sessions"] += 1
        p["active"] += total or 0
        a = json.loads(active or "{}")
        for f, s in (a.get("format") or {}).items():
            p["fmt_all"][f] = p["fmt_all"].get(f, 0) + s
        for key, s in (a.get("combo") or {}).items():
            f, _, ph = (key.split("|") + ["", ""])[:3]
            if ph == "libre":
                p["fmt_free"][f] = p["fmt_free"].get(f, 0) + s
        p["ua"] = json.loads(ua or "{}")
        if iph:
            p["ip"].add(iph)
    session_owner = dict(con.execute("SELECT session_id, visitor_id FROM sessions"))
    for sid, typ, data in con.execute(
            "SELECT session_id, type, data FROM events WHERE type IN ('format','mission_done','mission_skip') ORDER BY received, seq, idx"):
        vid = session_owner.get(sid)
        if not vid:
            continue
        p = person(vid)
        d = json.loads(data)
        if typ == "format":
            f = d.get("v")
            p["manual"][f] = p["manual"].get(f, 0) + 1
        else:
            key = (d.get("format"), d.get("m"))
            p["missions"][key] = d  # last attempt wins (a person can redo the series)
    for vid, vote, created, updated, revisions, iph in con.execute(
            "SELECT visitor_id, vote, created, updated, revisions, ip_hash FROM votes"):
        p = person(vid)
        p["vote"] = json.loads(vote)
        p["revisions"] = revisions
        p["voted_at"] = updated
        if p["first_seen"] is None:
            p["first_seen"] = created
        if iph:
            p["ip"].add(iph)
    return people


def _aggregate(people):
    fm = {f: {"pref": 0, "least": 0, "ratings": {k: [] for k in RATINGS}, "free": 0, "all": 0, "top_free": 0, "manual": 0,
              "m_ok": 0, "m_n": 0, "m_time": [], "m_wrong": [], "by_m": {m: [] for m in MISSIONS}} for f in FORMATS}
    colors, roles = {}, {}
    agree = disagree = 0
    for p in people:
        v = p["vote"] or {}
        if v.get("pref") in fm:
            fm[v["pref"]]["pref"] += 1
            for k, n in (v.get("ratings") or {}).items():
                if k in RATINGS:
                    fm[v["pref"]]["ratings"][k].append(n)
        if v.get("least") in fm:
            fm[v["least"]]["least"] += 1
        if v.get("color"):
            colors[v["color"]] = colors.get(v["color"], 0) + 1
        if v.get("role"):
            roles[v["role"]] = roles.get(v["role"], 0) + 1
        for f, s in p["fmt_free"].items():
            if f in fm:
                fm[f]["free"] += s
        for f, s in p["fmt_all"].items():
            if f in fm:
                fm[f]["all"] += s
        for f, n in p["manual"].items():
            if f in fm:
                fm[f]["manual"] += n
        top = max(p["fmt_free"].items(), key=lambda kv: kv[1], default=(None, 0))
        if top[0] in fm and top[1] >= 20:
            fm[top[0]]["top_free"] += 1
            if v.get("pref"):
                agree, disagree = (agree + 1, disagree) if v["pref"] == top[0] else (agree, disagree + 1)
        for (f, m), r in p["missions"].items():
            if f not in fm:
                continue
            fm[f]["m_n"] += 1
            if r.get("ok"):
                fm[f]["m_ok"] += 1
                secs = (r.get("ms") or 0) / 1000
                fm[f]["m_time"].append(secs)
                if m in MISSIONS:
                    fm[f]["by_m"][m].append(secs)
            fm[f]["m_wrong"].append(r.get("wrong") or 0)
    out = {}
    for f, a in fm.items():
        out[f] = {
            "name": FORMATS[f], "pref": a["pref"], "least": a["least"],
            "ratings": {k: (round(sum(x) / len(x), 2) if x else None) for k, x in a["ratings"].items()},
            "free_s": a["free"], "all_s": a["all"], "top_free": a["top_free"], "manual": a["manual"],
            "missions": a["m_n"], "success": (round(100 * a["m_ok"] / a["m_n"]) if a["m_n"] else None),
            "median_s": _median(a["m_time"]), "wrong_avg": (round(sum(a["m_wrong"]) / len(a["m_wrong"]), 2) if a["m_wrong"] else None),
            "by_mission": {m: _median(x) for m, x in a["by_m"].items()},
        }
    return {"formats": out, "colors": colors, "roles": roles, "agree": agree, "disagree": disagree,
            "people": len(people), "voters": sum(1 for p in people if p["vote"])}


def compute(con: sqlite3.Connection) -> dict:
    people = sorted(_load(con).values(), key=lambda p: p["first_seen"] or 0)
    ip_count: dict[str, int] = {}
    for p in people:
        if p["vote"]:
            for h in p["ip"]:
                ip_count[h] = ip_count.get(h, 0) + 1
    rows, comments = [], []
    for i, p in enumerate(people, 1):
        v = p["vote"] or {}
        top = max(p["fmt_free"].items(), key=lambda kv: kv[1], default=(None, 0))
        oks = [r for r in p["missions"].values() if r.get("ok")]
        rows.append({
            "n": i, "name": v.get("name", ""), "role": v.get("role", ""), "pref": v.get("pref", ""), "least": v.get("least", ""),
            "color": v.get("color", ""), "ratings": v.get("ratings", {}), "active_s": p["active"], "sessions": p["sessions"],
            "fmt_free": p["fmt_free"], "fmt_all": p["fmt_all"], "manual": p["manual"], "top_free": top[0] if top[1] >= 20 else None,
            "missions_done": len(p["missions"]), "missions_ok": len(oks),
            "missions_median_s": _median([(r.get("ms") or 0) / 1000 for r in oks]),
            "revisions": p["revisions"], "voted_at": p["voted_at"], "first_seen": p["first_seen"],
            "same_network": any(ip_count.get(h, 0) > 1 for h in p["ip"]) if p["vote"] else False,
            "device": "mobile" if (p["ua"] or {}).get("coarse") else "ordinateur", "width": (p["ua"] or {}).get("w"),
        })
        if v.get("comment"):
            comments.append({"n": i, "name": v.get("name", ""), "role": v.get("role", ""), "pref": v.get("pref", ""), "text": v["comment"]})
    metier = [p for p in people if (p["vote"] or {}).get("role") in METIER]
    return {"generated": time.time(), "all": _aggregate(people), "metier": _aggregate(metier), "people": rows, "comments": comments,
            "labels": {"formats": FORMATS, "missions": MISSIONS, "ratings": RATINGS}}


def page() -> str:
    return PAGE


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Résultats — essai du panneau LeClasseur</title>
<style>
body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, "Segoe UI", sans-serif; background: #eef2f1; color: #10201f; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 20px 60px; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 30px 0 10px; }
.sub { color: #5b6b68; font-size: 13.5px; }
.cards { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; }
.c { background: #fff; border-radius: 12px; padding: 12px 16px; min-width: 150px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.c b { display: block; font-size: 24px; } .c span { font-size: 12.5px; color: #5b6b68; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); font-size: 13.5px; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid #edf0ef; vertical-align: top; }
th { font-size: 11.5px; text-transform: uppercase; letter-spacing: .05em; color: #6b7b78; background: #f7f9f8; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.lead td:first-child { font-weight: 700; }
.best { background: #e3f4ea; font-weight: 700; }
.bar { display: inline-block; height: 8px; background: #146874; border-radius: 4px; vertical-align: middle; margin-right: 6px; }
.toggle { display: inline-flex; gap: 6px; margin-top: 12px; }
.toggle button { padding: 6px 12px; border-radius: 999px; border: 1px solid #cfd8d6; background: #fff; cursor: pointer; font: inherit; font-size: 13px; }
.toggle button.on { background: #146874; color: #fff; border-color: #146874; }
.verdict { background: #fff; border-radius: 12px; padding: 14px 16px; margin-top: 14px; font-size: 14.5px; line-height: 1.6; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.comment { background: #fff; border-radius: 10px; padding: 10px 14px; margin-bottom: 8px; font-size: 14px; line-height: 1.5; }
.comment small { color: #6b7b78; display: block; margin-bottom: 3px; }
.flag { color: #b86e00; font-weight: 700; }
.muted { color: #8a9997; }
</style></head><body><main>
<h1>Résultats de l'essai du panneau</h1>
<div class="sub" id="gen"></div>
<div class="toggle"><button data-g="all" class="on">Tout le monde</button><button data-g="metier">Courtiers et adjoints seulement</button></div>
<div class="cards" id="cards"></div>
<div class="verdict" id="verdict"></div>
<h2>Ce que les gens disent (leur avis)</h2><table id="said"></table>
<h2>Ce que les gens font (télémétrie)</h2><table id="did"></table>
<p class="sub">« Temps libre » : temps actif passé sur un format quand la personne choisit elle-même (hors missions). « Format le plus regardé » : nombre de personnes dont c'est le format où elles ont passé le plus de temps libre (20 s minimum). Temps actif = page visible et souris/clavier utilisés dans les 30 dernières secondes.</p>
<h2>Missions : temps médian pour réussir (secondes, plus bas = mieux)</h2><table id="mis"></table>
<h2>Par personne</h2><table id="ppl"></table>
<h2>Commentaires</h2><div id="com"></div>
<h2>Secondaire</h2><table id="sec"></table>
</main>
<script>
let DATA = null, G = "all";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const mmss = (s) => s == null ? "—" : s < 60 ? Math.round(s) + " s" : Math.floor(s / 60) + " min " + String(Math.round(s % 60)).padStart(2, "0");
const ROLE = { courtier: "Courtier", adjoint: "Adjoint(e)", immobilier: "Immobilier / prêt", autre: "Autre" };
function bestKey(obj, fn, low) { let best = null, bv = null; for (const [k, v] of Object.entries(obj)) { const x = fn(v); if (x == null || (low ? false : x === 0)) continue; if (bv == null || (low ? x < bv : x > bv)) { bv = x; best = k; } } return best; }
function render() {
    const A = DATA[G], F = A.formats, L = DATA.labels;
    $("gen").textContent = "Mis à jour " + new Date(DATA.generated * 1000).toLocaleString("fr-CA") + " · se rafraîchit toutes les 30 secondes";
    const tot = DATA.people.filter((p) => G === "all" || ["courtier", "adjoint"].includes(p.role));
    $("cards").innerHTML = [["Personnes", A.people], ["Avis donnés", A.voters], ["Temps actif total", mmss(tot.reduce((a, p) => a + p.active_s, 0))], ["Missions faites", tot.reduce((a, p) => a + p.missions_done, 0)]].map(([l, v]) => `<div class="c"><b>${v}</b><span>${l}</span></div>`).join("");
    const bPref = bestKey(F, (f) => f.pref), bFree = bestKey(F, (f) => f.free_s), bTop = bestKey(F, (f) => f.top_free), bFast = bestKey(F, (f) => f.median_s, true);
    const n = (k) => k ? "<b>" + esc(F[k].name) + "</b>" : "<span class='muted'>pas encore assez de données</span>";
    $("verdict").innerHTML = `Format préféré dans les avis : ${n(bPref)}<br>Format le plus regardé (temps libre) : ${n(bFree)}${bTop ? " · format favori par le temps pour le plus de personnes : " + n(bTop) : ""}<br>Format le plus rapide aux missions : ${n(bFast)}<br>` +
        (A.agree + A.disagree ? `Les gens ont choisi le format qu'ils ont le plus regardé : <b>${A.agree} sur ${A.agree + A.disagree}</b>.` : "");
    const max = (k) => Math.max(1, ...Object.values(F).map((f) => f[k] || 0));
    const cls = (k, best) => k === best ? " class='best'" : "";
    $("said").innerHTML = "<tr><th>Format</th><th>Préféré</th><th>Le moins aimé</th>" + Object.values(L.ratings).map((r) => `<th>${r} /5</th>`).join("") + "</tr>" +
        Object.entries(F).map(([k, f]) => `<tr class="lead"><td>${esc(f.name)}</td><td${cls(k, bPref)}><span class="bar" style="width:${60 * f.pref / max("pref")}px"></span>${f.pref}</td><td>${f.least}</td>` +
            Object.keys(L.ratings).map((r) => `<td class="num">${f.ratings[r] ?? "—"}</td>`).join("") + "</tr>").join("");
    $("did").innerHTML = "<tr><th>Format</th><th>Temps libre</th><th>Format le plus regardé de… (pers.)</th><th>Choisi à la main (fois)</th><th>Missions réussies</th><th>Temps médian mission</th><th>Clics à côté (moy.)</th></tr>" +
        Object.entries(F).map(([k, f]) => `<tr class="lead"><td>${esc(f.name)}</td><td${cls(k, bFree)}><span class="bar" style="width:${60 * f.free_s / max("free_s")}px"></span>${mmss(f.free_s)}</td><td${cls(k, bTop)}>${f.top_free}</td><td class="num">${f.manual}</td><td class="num">${f.success == null ? "—" : f.success + " %"} <span class="muted">(${f.missions})</span></td><td${cls(k, bFast)}>${mmss(f.median_s)}</td><td class="num">${f.wrong_avg ?? "—"}</td></tr>`).join("");
    $("mis").innerHTML = "<tr><th>Format</th>" + Object.values(L.missions).map((m) => `<th>${m}</th>`).join("") + "</tr>" +
        Object.entries(F).map(([k, f]) => `<tr class="lead"><td>${esc(f.name)}</td>` + Object.keys(L.missions).map((m) => { const b = bestKey(F, (x) => x.by_mission[m], true); return `<td${cls(k, b)}>${f.by_mission[m] ?? "—"}</td>`; }).join("") + "</tr>").join("");
    $("ppl").innerHTML = "<tr><th>#</th><th>Prénom</th><th>Rôle</th><th>Préféré</th><th>Moins aimé</th><th>Plus regardé</th><th>Temps actif</th><th>Missions</th><th>Médiane</th><th>Visites</th><th>Modifs</th><th>Appareil</th></tr>" +
        tot.map((p) => `<tr><td>${p.n}</td><td>${esc(p.name) || "<span class='muted'>—</span>"}</td><td>${ROLE[p.role] || "<span class='muted'>pas d'avis</span>"}</td><td>${esc(L.formats[p.pref] || "")}</td><td>${esc(L.formats[p.least] || "")}</td><td>${esc(L.formats[p.top_free] || "")}${p.pref && p.top_free && p.pref !== p.top_free ? " <span class='flag' title='Préfère un format, mais en regarde surtout un autre'>≠</span>" : ""}</td><td class="num">${mmss(p.active_s)}</td><td class="num">${p.missions_ok}/${p.missions_done}</td><td class="num">${mmss(p.missions_median_s)}</td><td class="num">${p.sessions}</td><td class="num">${p.revisions > 1 ? p.revisions - 1 : ""}</td><td>${p.device}${p.same_network ? " <span class='flag' title='Un autre avis vient du même réseau internet'>même réseau</span>" : ""}</td></tr>`).join("");
    const cm = DATA.comments.filter((c) => G === "all" || ["courtier", "adjoint"].includes(c.role));
    $("com").innerHTML = cm.length ? cm.map((c) => `<div class="comment"><small>#${c.n} ${esc(c.name)} · ${ROLE[c.role] || ""} · préfère ${esc(L.formats[c.pref] || "?")}</small>${esc(c.text)}</div>`).join("") : "<p class='sub'>Aucun commentaire pour l'instant.</p>";
    $("sec").innerHTML = "<tr><th>Couleur préférée</th><th>Avis</th></tr>" + Object.entries(A.colors).sort((a, b) => b[1] - a[1]).map(([c, n]) => `<tr><td>${esc(c)}</td><td class="num">${n}</td></tr>`).join("") +
        "<tr><th>Rôle</th><th>Avis</th></tr>" + Object.entries(A.roles).map(([r, n]) => `<tr><td>${ROLE[r] || esc(r)}</td><td class="num">${n}</td></tr>`).join("");
}
async function load() { try { const r = await fetch("/api/results", { cache: "no-store" }); DATA = await r.json(); render(); } catch (e) { $("gen").textContent = "Erreur de chargement : " + e; } }
document.querySelectorAll(".toggle button").forEach((b) => b.onclick = () => { G = b.dataset.g; document.querySelectorAll(".toggle button").forEach((x) => x.classList.toggle("on", x === b)); render(); });
load(); setInterval(load, 30000);
</script></body></html>
"""
