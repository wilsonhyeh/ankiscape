"""Offline game handbook generated from the shipped economy and thresholds."""
from html import escape
from ..logic_pure import multiplied_base_micro, gathering_probability, burn_probability


def credits_page(rules=None):
    """Offline Credits & assets page built from the shipped manifest.

    Every bundled asset's source, revision, retrieval date, size and hash is
    listed here; the Jagex/Wiki rights notice is preserved verbatim. No
    network access: the manifest ships inside the add-on.
    """
    import json
    import os
    from ..icons import repo_root

    path = os.path.join(repo_root(), "assets", "manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return ("<h1>Credits &amp; assets</h1>"
                "<p>Asset manifest unavailable in this installation.</p>")
    _ = rules
    notices = manifest.get("rights_notices", {})
    parts = ["<h1>Credits &amp; assets</h1>",
             "<p>Everything bundled here is used offline. The add-on never "
             "downloads images while you play.</p>"]
    for key in ("jagex-wiki", "original", "ofl"):
        notice = notices.get(key) or {}
        text = escape(str(notice.get("notice") or ""))
        issue = escape(str(notice.get("release_issue") or ""))
        parts.append(f"<h2>{escape(key)}</h2><p>{text}</p>")
        if issue:
            parts.append(f"<p><b>Release note:</b> {issue}</p>")
    parts.append("<h2>Bundled files</h2>")
    parts.append("<ul>")
    for record in manifest.get("files", []):
        if not isinstance(record, dict):
            continue
        rel = escape(str(record.get("path") or ""))
        display = escape(str(record.get("display") or ""))
        source = escape(str(record.get("source_page") or
                           record.get("origin") or ""))
        revision = escape(str(record.get("revision") or ""))
        retrieved = escape(str(record.get("retrieved") or ""))
        width = record.get("width")
        height = record.get("height")
        size = f"{width}x{height}" if width and height else ""
        digest = escape(str(record.get("sha256") or "")[:12])
        line = f"<li><b>{display}</b> — <code>{rel}</code>"
        if source:
            line += f" — source: {source}"
        if revision:
            line += f" (rev {revision})"
        if retrieved:
            line += f", retrieved {retrieved}"
        if size:
            line += f", {size}"
        if digest:
            line += f", sha256 {digest}…"
        line += "</li>"
        parts.append(line)
    parts.append("</ul>")
    return "".join(parts)


def guide_pages(rules):
    pages = {}
    pages['Getting started'] = '''<h1>AnkiScape skill guide</h1>
<p>Train one skill while reviewing in Anki. Select a skill and resource in Skills,
then press Train. Browsing resources does not change your active training.</p>
<h2>Which answers earn rewards?</h2><p>Hard, Good and Easy each make one attempt.
Again earns nothing. Rating Easy does not give bonus XP. Reviews before you finish
Evolved setup earn no Evolved rewards. Undo removes the associated reward; redo restores
it. Repeating sync cannot award the same review twice.</p>
<h2>XP and levels</h2><p>Successful XP = base XP × min(1 + 0.05 × (tier − 1), 2).
Tier is the resource's position in its skill table, not your level. XP retains six
decimal places, with half-up rounding. All six skills start at level 1 and cap at 99.
XP can continue growing after 99.</p>
<p>A failed gathering attempt or burned fish awards 25% of successful XP, with a
minimum of 1 XP. Missing ingredients or a lost required level awards zero XP and
consumes nothing. Training stays selected and resumes when its requirements are met.</p>
<h2>First production recipes</h2><p>Mine one Copper ore and one Tin ore, then smelt a
Bronze bar. Mine Clay, craft Soft clay, then an Unfired pot and a Pot. Catch Shrimp,
then cook it. Each conversion takes another accepted review.</p>
<p>The tables show exact successful XP before mining gem bonuses. They describe
Evolved; Classic keeps its separate original rules and progress.</p>'''
    specs = [('Mining','ores','level','base_xp'), ('Woodcutting','trees','level','base_xp'),
             ('Fishing','fish','fishing_level','fishing_base_xp'), ('Smithing','bars','level','base_xp'),
             ('Crafting','crafting','level','base_xp'), ('Cooking','fish','cooking_level','cooking_base_xp')]
    for title, table, level, xp in specs:
        gathering = title in ('Mining','Woodcutting','Fishing')
        body = f'<h1>{title}</h1>'
        if gathering:
            body += '<p>Success chance = min(80% + 2% × your skill level, 95%) × resource probability. Success gives one item; failure gives no item and reduced XP. Higher tiers do not guarantee better XP per review.</p>'
        elif title == 'Cooking':
            body += '<p>Each attempt consumes one raw fish. Success produces one Cooked fish. Burning produces no item. Burn chance = max(30% − 0.2% × Cooking level, 0%). At level 99 it is still 10.2%. Cooked food is a collectible; eating and equipment bonuses are not implemented.</p>'
        else:
            body += '<p>One accepted review consumes the listed ingredients and produces one output. There is no random failure when your level and materials meet the recipe. Ingredients come from your Bank.</p>'
        body += '<table border="1" cellpadding="5"><tr><th>Resource / output</th><th>Level</th><th>Tier</th><th>Base XP</th><th>Success XP</th><th>Requirements / chance at unlock</th></tr>'
        for item in rules[table]:
            if gathering:
                detail = f'{float(gathering_probability(item[level], item["probability"]))*100:.2f}% success'
            elif title == 'Cooking':
                detail = f'1 × {item["display"]}; {float(burn_probability(item[level]))*100:.1f}% burn'
            else:
                detail = ', '.join(f'{n} × {name}' for name,n in item.get('requirements',item.get('ore_required',{})).items())
            amount = multiplied_base_micro(item[xp],item['tier']) / 1_000_000
            body += '<tr>' + ''.join(f'<td>{escape(str(v))}</td>' for v in (item['display'],item[level],item['tier'],item[xp],f'{amount:g}',detail)) + '</tr>'
        pages[title] = body + '</table>'
    pages['Gems'] = '<h1>Mining gems</h1><p>After successful mining, a 1-in-256 gem roll is followed by the distribution below. The remaining distribution gives no gem. A gem is extra loot alongside the ore; its bonus Mining XP uses the selected ore tier multiplier. Cut gems through Crafting before using them in jewelry.</p><table border="1" cellpadding="5"><tr><th>Gem</th><th>Chance after gem roll</th><th>Bonus base XP</th></tr>' + ''.join(f'<tr><td>{escape(g["display"])}</td><td>{g["probability_num"]}/{g["probability_den"]}</td><td>{g["base_xp"]}</td></tr>' for g in rules['gems']) + '</table>'
    pages['Level table'] = '<h1>Levels 1–99</h1><p>Cumulative XP thresholds apply separately to every skill. Total level is the sum of six skill levels (6–594).</p><table border="1" cellpadding="5"><tr><th>Level</th><th>Total XP</th><th>XP since previous level</th></tr>' + ''.join(f'<tr><td>{i+1}</td><td>{xp:,}</td><td>{xp-(rules["thresholds"][i-1] if i else 0):,}</td></tr>' for i,xp in enumerate(rules['thresholds'])) + '</table>'
    pages['Bank & achievements'] = '''<h1>Bank and achievements</h1><p>The Bank shows your current inventory. Production consumes ingredients automatically. Filtering or inspecting items does not spend them. There is no trading, selling, equipping, or item-use action.</p><p>Achievements track first catch, first successful cook, skill levels 10/30/60/99 in each skill, and 100/1,000 successful cooks. Spending inventory does not remove a successful-action achievement. Undo and sync reconciliation can remove an achievement if its underlying review or level no longer qualifies. Achievements award no additional XP.</p>'''
    pages['Catch-up, sync & accounts'] = '''<h1>Catch-up and online progress</h1><p>Play offline without an account. Reviews synced from mobile are processed on desktop using your gathering preset (Mining, Woodcutting or Fishing), at the highest unlocked tier when replayed. This preset is separate from desktop training. Changes apply from their recorded time onward.</p><p>Reviews before Evolved activation and reviews observed in Classic do not receive catch-up rewards. Direct desktop rewards take priority over a catch-up claim for the same review.</p><h2>Accounts and Hiscores</h2><p>Create an account and enter the email verification code to enable online progress. Log in by username or email. One account links to one Evolved game. Existing offline progress can upload. The server calculates ranking XP from the review history rather than trusting an uploaded XP total.</p><p>When devices spend the same ingredients offline, the merged review order determines which recipe has materials. Invalidated production gets zero reward, so totals and achievements can change after sync. Sync now retries pending uploads; local play continues during an outage.</p><h2>Sign-in and recovery</h2><p>Keep me signed in uses the operating system credential vault when available. Your password is never stored. Log out removes saved sign-in credentials without deleting progress. Forgot password requests an email code, followed by a new password.</p>'''
    pages['Modes, settings & backups'] = '''<h1>Classic and Evolved</h1><p>Classic preserves your original game. Evolved starts a separate six-skill game; XP and items do not transfer. Use the AnkiScape toolbar menu to open the active mode or try Evolved. Evolved Settings → Advanced lets you return to Classic. Leave the reviewer before switching. Closing setup saves its place; Continue Classic lets an upgrading user return immediately.</p><h2>Settings and review HUD</h2><p>Appearance controls scale, HUD position and visibility, celebrations, reduced motion and level-up sounds. The HUD shows active skill progress during reviews. The session recap describes rewards from that study session. Settings apply immediately.</p><h2>Backups</h2><p>Export backup saves your Evolved game history as JSON. Store this file somewhere safe. Restore previews the change and asks for confirmation. A backup for the current game merges missing history. A different game opens as the active Evolved game and signs you out; the old game remains saved. Neither action replaces Anki cards. Export is separate from account sync and Anki collection backups. Advanced diagnostics help explain pending sync problems.</p>'''
    pages['Credits & assets'] = credits_page(rules)
    return pages


def build_guide_screen(shell, deps):
    from aqt.qt import QWidget, QVBoxLayout, QComboBox, QLineEdit, QTextBrowser, QTextCursor
    root = QWidget(shell)
    root.setObjectName('ankiscape-guide')
    layout = QVBoxLayout(root)
    topics = QComboBox()
    topics.setObjectName('ankiscape-guide-topics')
    search = QLineEdit()
    search.setPlaceholderText('Find in this article…')
    search.setObjectName('ankiscape-guide-search')
    browser = QTextBrowser()
    browser.setObjectName('ankiscape-guide-article')
    pages = guide_pages(shell.call('get_rules', default={}) or {})
    topics.addItems(list(pages))
    layout.addWidget(topics)
    layout.addWidget(search)
    layout.addWidget(browser, 1)
    topics.currentTextChanged.connect(lambda title: browser.setHtml(pages[title]))
    search.returnPressed.connect(lambda: browser.find(search.text()) or (browser.moveCursor(QTextCursor.MoveOperation.Start), browser.find(search.text())))
    browser.setHtml(pages[topics.currentText()])
    root.refresh = lambda: None
    root.on_show = lambda: None
    root.invalidate = lambda: None
    root.release = lambda: None
    return root
