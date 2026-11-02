"""
Veille réglementaire NIS 2 - collecte automatique (open source, gratuit).

Lit sources.yaml, puis :
  - flux RSS / newsletters : garde les articles qui contiennent les mots-clés
  - pages sans RSS : signale quand le contenu de la page a changé
Produit :
  - rapport.md          (lisible directement sur GitHub)
  - docs/index.html     (même rapport en page web, via GitHub Pages)
  - etat.json           (mémoire de l'outil : articles déjà vus, empreintes des pages)
"""
import hashlib
import html
import json
import os
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

DOSSIER = os.path.dirname(os.path.abspath(__file__))
FICHIER_ETAT = os.path.join(DOSSIER, "etat.json")
JOURS_CONSERVES = 60
ENTETES = {"User-Agent": "Mozilla/5.0 (veille-nis2; +open-source)"}


def charger_etat():
    etat = {"articles": {}, "pages": {}, "statut": {}}
    if os.path.exists(FICHIER_ETAT):
        with open(FICHIER_ETAT, encoding="utf-8") as f:
            etat.update(json.load(f))
    etat.setdefault("statut", {})
    return etat


def noter_statut(etat, source, ok, change=False):
    maintenant = datetime.now(timezone.utc).isoformat()
    s = etat["statut"].setdefault(source["url"], {"depuis": maintenant, "dernier_changement": None})
    s["ok"] = ok
    s["derniere_verif"] = maintenant
    if change:
        s["dernier_changement"] = maintenant


def themes_trouves(texte, themes):
    texte = texte.lower()
    return [t for t, mots in themes.items() if any(m.lower() in texte for m in mots)]


def date_article(entree):
    for cle in ("published_parsed", "updated_parsed"):
        if entree.get(cle):
            return datetime(*entree[cle][:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def traiter_rss(source, themes, etat, erreurs):
    try:
        reponse = requests.get(source["url"], headers=ENTETES, timeout=30)
        reponse.raise_for_status()
        flux = feedparser.parse(reponse.content)
    except Exception as e:
        erreurs.append(f"{source['nom']} : {e}")
        noter_statut(etat, source, False)
        return
    noter_statut(etat, source, True)
    for entree in flux.entries:
        lien = entree.get("link") or entree.get("id")
        if not lien or lien in etat["articles"]:
            continue
        titre = entree.get("title", "(sans titre)")
        resume = BeautifulSoup(entree.get("summary", ""), "html.parser").get_text(" ")
        trouves = themes_trouves(f"{titre} {resume}", themes)
        if not trouves:
            continue
        etat["articles"][lien] = {
            "titre": titre,
            "source": source["nom"],
            "pays": source["pays"],
            "themes": trouves,
            "date": date_article(entree).isoformat(),
            "type": "article",
        }


def traiter_page(source, themes, etat, erreurs):
    try:
        reponse = requests.get(source["url"], headers=ENTETES, timeout=30)
        reponse.raise_for_status()
    except Exception as e:
        erreurs.append(f"{source['nom']} : {e}")
        noter_statut(etat, source, False)
        return
    soupe = BeautifulSoup(reponse.text, "html.parser")
    for balise in soupe(["script", "style", "nav", "header", "footer"]):
        balise.decompose()
    texte = " ".join(soupe.get_text(" ").split())
    empreinte = hashlib.sha256(texte.encode()).hexdigest()
    ancienne = etat["pages"].get(source["url"])
    etat["pages"][source["url"]] = empreinte
    noter_statut(etat, source, True, change=ancienne is not None and ancienne != empreinte)
    if ancienne is None or ancienne == empreinte:
        return  # première visite (référence enregistrée) ou aucun changement
    maintenant = datetime.now(timezone.utc)
    cle = f"{source['url']}#{maintenant:%Y-%m-%d}"
    etat["articles"][cle] = {
        "titre": f"La page « {source['nom']} » a été modifiée",
        "lien": source["url"],
        "source": source["nom"],
        "pays": source["pays"],
        "themes": themes_trouves(texte, themes) or ["(à vérifier)"],
        "date": maintenant.isoformat(),
        "type": "page modifiée",
    }


def nettoyer(etat):
    limite = datetime.now(timezone.utc) - timedelta(days=JOURS_CONSERVES)
    etat["articles"] = {
        k: v for k, v in etat["articles"].items()
        if datetime.fromisoformat(v["date"]) >= limite
    }


def ecrire_rapports(etat, erreurs, sources):
    articles = sorted(
        ({"lien": v.get("lien", k), **v} for k, v in etat["articles"].items()),
        key=lambda a: a["date"], reverse=True,
    )
    pays_ordre = list(dict.fromkeys(s["pays"] for s in sources))
    maj = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    # ---- Markdown ----
    md = [f"# Veille NIS 2", f"_Mise à jour : {maj} — {len(articles)} élément(s) sur {JOURS_CONSERVES} jours_\n"]
    for pays in pays_ordre:
        lot = [a for a in articles if a["pays"] == pays]
        if not lot:
            continue
        md.append(f"## {pays}\n")
        md.append("| Date | Thème | Source | Titre |")
        md.append("|---|---|---|---|")
        for a in lot:
            titre = a["titre"].replace("|", "-")
            md.append(f"| {a['date'][:10]} | {', '.join(a['themes'])} | {a['source']} | [{titre}]({a['lien']}) |")
        md.append("")
    md.append(f"## Sources surveillées ({len(sources)})\n")
    md.append("| Pays | Source | État |")
    md.append("|---|---|---|")
    for s in sources:
        ok = etat["statut"].get(s["url"], {}).get("ok")
        md.append(f"| {s['pays']} | [{s['nom']}]({s['url']}) | {'✅' if ok else '❌'} |")
    md.append("")
    if erreurs:
        md.append("## Sources inaccessibles lors de la dernière collecte\n")
        md += [f"- {e}" for e in erreurs]
    with open(os.path.join(DOSSIER, "rapport.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")

    # ---- HTML ----
    lignes = "".join(
        f"<tr data-pays='{html.escape(a['pays'])}'><td>{a['date'][:10]}</td><td>{html.escape(a['pays'])}</td>"
        f"<td>{html.escape(', '.join(a['themes']))}</td><td>{html.escape(a['source'])}</td>"
        f"<td><a href='{html.escape(a['lien'])}' target='_blank'>{html.escape(a['titre'])}</a>"
        f"{' <em>(page modifiée)</em>' if a['type'] == 'page modifiée' else ''}</td></tr>"
        for a in articles
    )
    options = "".join(f"<option>{html.escape(p)}</option>" for p in pays_ordre)
    err = "".join(f"<li>{html.escape(e)}</li>" for e in erreurs)

    def jour(iso):
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y") if iso else ""

    lignes_sources = ""
    for s in sources:
        st = etat["statut"].get(s["url"], {})
        ok = st.get("ok")
        etat_txt = "✅ OK" if ok else ("❌ Inaccessible" if ok is False else "⏳ En attente")
        if s["type"] == "page":
            methode = "Surveillance de page"
            suivi = (f"Dernier changement : {jour(st['dernier_changement'])}" if st.get("dernier_changement")
                     else f"Aucun changement depuis le {jour(st.get('depuis'))}" if st.get("depuis") else "")
        else:
            methode = "Newsletter" if "kill-the-newsletter" in s["url"] else "Flux RSS"
            nb = sum(1 for a in articles if a["source"] == s["nom"])
            suivi = f"{nb} élément(s) retenu(s)"
        lignes_sources += (
            f"<tr data-pays='{html.escape(s['pays'])}'><td>{html.escape(s['pays'])}</td>"
            f"<td><a href='{html.escape(s['url'])}' target='_blank'>{html.escape(s['nom'])}</a></td>"
            f"<td>{methode}</td><td>{etat_txt}</td><td>{suivi}</td></tr>"
        )
    page = f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Veille NIS 2</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}}
th{{background:#f4f4f4}}select{{padding:.3rem;margin:.5rem 0}}.err{{color:#a33}}</style></head><body>
<h1>Veille réglementaire NIS 2</h1><p>Mise à jour : {maj} — {len(articles)} élément(s) sur {JOURS_CONSERVES} jours</p>
<label>Pays : <select id="f"><option value="">Tous</option>{options}</select></label>
<table><thead><tr><th>Date</th><th>Pays</th><th>Thème</th><th>Source</th><th>Titre</th></tr></thead><tbody>{lignes}</tbody></table>
<h2>Sources surveillées ({len(sources)})</h2>
<table><thead><tr><th>Pays</th><th>Source</th><th>Méthode</th><th>État</th><th>Suivi</th></tr></thead><tbody>{lignes_sources}</tbody></table>
{f'<h2>Détail des erreurs</h2><ul class="err">{err}</ul>' if erreurs else ''}
<script>document.getElementById('f').onchange=e=>document.querySelectorAll('tbody tr').forEach(r=>r.style.display=!e.target.value||r.dataset.pays===e.target.value?'':'none')</script>
</body></html>"""
    os.makedirs(os.path.join(DOSSIER, "docs"), exist_ok=True)
    with open(os.path.join(DOSSIER, "docs", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def main():
    with open(os.path.join(DOSSIER, "sources.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f)
    etat, erreurs = charger_etat(), []
    for source in config["sources"]:
        if source["type"] == "rss":
            traiter_rss(source, config["themes"], etat, erreurs)
        else:
            traiter_page(source, config["themes"], etat, erreurs)
    nettoyer(etat)
    with open(FICHIER_ETAT, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=1)
    ecrire_rapports(etat, erreurs, config["sources"])
    print(f"{len(etat['articles'])} élément(s), {len(erreurs)} erreur(s)")


if __name__ == "__main__":
    main()
