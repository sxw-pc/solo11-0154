"""Offline reception ledger and correction queries for a synthetic site bulletin.

The module never sends anything off site.  It records locally observed evidence:

* relay summary fragments giving an aggregate *received_count* over a group of
  workstation screens for one bulletin edition;
* direct terminal confirmations witnessed by the duty officer;
* independent read-backs of the board a screen currently holds;
* pre-registered authorizations for two distinct correction procedures;
* issued correction commands and the device occupation confirmations that prove
  a board was actually replaced.

Three facts are deliberately kept apart: role authorization for an edition,
which screen physically occupies which edition, and whether a specific delivery
was confirmed.  A submitted command is only a command; nothing counts as
delivered until a device confirmation is recorded.
"""

from .source import record
from .store import history

# Evidence kinds kept in the tamper-evident evidence table.
KIND_FRAGMENT = "reception-fragment"
KIND_TERMINAL = "terminal-confirmation"
KIND_READBACK = "board-readback"
KIND_AUTHORIZATION = "correction-authorization"
KIND_COMMAND = "correction-command"
KIND_OCCUPATION = "occupation-confirmation"


def initialize(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS bulletin (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            edition INTEGER NOT NULL,
            confirmation_target TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS bulletin_audience (
            bulletin_id TEXT NOT NULL,
            terminal TEXT NOT NULL,
            PRIMARY KEY (bulletin_id, terminal)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS device_registration (
            terminal TEXT PRIMARY KEY,
            supports_neutral INTEGER NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS relay_fragment (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            edition INTEGER NOT NULL,
            relay TEXT NOT NULL,
            window_start TEXT,
            window_end TEXT,
            recipients TEXT NOT NULL,
            received_count INTEGER NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS terminal_confirmation (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            edition INTEGER NOT NULL,
            terminal TEXT NOT NULL,
            confirmed INTEGER NOT NULL,
            witness TEXT NOT NULL,
            at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS board_readback (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            terminal TEXT NOT NULL,
            edition INTEGER NOT NULL,
            board_serial TEXT NOT NULL,
            witness TEXT NOT NULL,
            at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS correction_authorization (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            edition INTEGER NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT,
            allowed_terminals TEXT NOT NULL,
            registered_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS correction_dispatch (
            command_id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            edition INTEGER NOT NULL,
            kind TEXT NOT NULL,
            authorization_id TEXT,
            old_edition INTEGER,
            board_serial TEXT,
            subject TEXT,
            targets TEXT NOT NULL,
            restricted_batch_ref TEXT,
            issued_at TEXT NOT NULL,
            status TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS terminal_delivery (
            command_id TEXT NOT NULL,
            terminal TEXT NOT NULL,
            delivered INTEGER NOT NULL,
            PRIMARY KEY (command_id, terminal)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS board_version_history (
            id TEXT PRIMARY KEY,
            batch TEXT NOT NULL,
            terminal TEXT NOT NULL,
            edition INTEGER NOT NULL,
            command_id TEXT,
            at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS denied_action (
            command_id TEXT PRIMARY KEY,
            batch TEXT,
            kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            reason TEXT NOT NULL,
            at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS reception_analysis (
            batch TEXT NOT NULL,
            edition INTEGER NOT NULL,
            terminal TEXT NOT NULL,
            verdict TEXT NOT NULL,
            PRIMARY KEY (batch, edition, terminal)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS target_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch TEXT NOT NULL,
            target TEXT NOT NULL,
            changed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )


def refresh(db, batch):
    initialize(db)
    transport = db.execute(
        "SELECT state,liable_party FROM transport WHERE batch=?", (batch,)
    ).fetchone()
    if not transport:
        target = "not_assigned"
    elif transport["state"] == "carrier_responsible":
        target = "carrier_console"
    elif transport["liable_party"] == "producer":
        target = "producer_console"
    else:
        target = "not_assigned"
    db.execute("UPDATE bulletin SET confirmation_target=? WHERE batch=?", (target, batch))
    last = db.execute(
        "SELECT target FROM target_history WHERE batch=? ORDER BY id DESC LIMIT 1",
        (batch,),
    ).fetchone()
    if last is None or last["target"] != target:
        db.execute(
            "INSERT INTO target_history(batch,target) VALUES (?,?)",
            (batch, target),
        )


def current_target(db, batch):
    """Role currently entitled to the batch's confirmation target."""
    row = db.execute(
        "SELECT target FROM target_history WHERE batch=? ORDER BY id DESC LIMIT 1",
        (batch,),
    ).fetchone()
    if row:
        return row["target"]
    row = db.execute(
        "SELECT confirmation_target FROM bulletin WHERE batch=? LIMIT 1",
        (batch,),
    ).fetchone()
    return row["confirmation_target"] if row else "not_assigned"


# --------------------------------------------------------------------------- #
# Import of locally collected evidence
# --------------------------------------------------------------------------- #

def _synthetic_id(prefix, item):
    import hashlib
    import json

    raw = json.dumps(item, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode()
    return prefix + "-" + hashlib.sha256(raw).hexdigest()[:16]


def _record_command(db, item):
    evidence = dict(item)
    evidence.setdefault("id", item["command_id"])
    return record(db, KIND_COMMAND, evidence)


def _fragments(data):
    """Relay fragments keep their original identity when present.

    The collated fixture predates explicit fragment ids, so a deterministic id
    is derived from the fragment payload; replaying the same original document
    therefore never creates a second, artificial delivery fact.  Legacy
    fragments also omit the batch, which is then taken from the single batch
    covered by the document's bulletins.
    """
    batches = sorted({b["batch"] for b in data.get("bulletins", [])})
    default_batch = batches[0] if len(batches) == 1 else None
    out = []
    for fragment in data.get("reception_constraints", []):
        item = dict(fragment)
        item.setdefault("relay", "RELAY-1")
        if "batch" not in item:
            if default_batch is None:
                raise ValueError("reception fragment without batch in multi-batch document")
            item["batch"] = default_batch
        item["id"] = fragment.get("id") or _synthetic_id("FRAG", item)
        out.append(item)
    return out


def import_data(db, data):
    batches = set()
    with db:
        initialize(db)
        for bulletin in data.get("bulletins", []):
            if record(db, "site-bulletin", bulletin):
                db.execute(
                    "INSERT INTO bulletin VALUES (?,?,?,?)",
                    (bulletin["id"], bulletin["batch"], bulletin["edition"],
                     "not_assigned"),
                )
            batches.add(bulletin["batch"])

        declared_audience = data.get("recipient_keys")
        for bulletin in data.get("bulletins", []):
            terminals = set(declared_audience or [])
            if not terminals:
                # An edition registered on its own inherits the audience of the
                # batch's earlier editions: the same three screens are in scope.
                terminals.update(r["terminal"] for r in db.execute(
                    """SELECT a.terminal FROM bulletin_audience a
                       JOIN bulletin b ON b.id = a.bulletin_id
                       WHERE b.batch=?""", (bulletin["batch"],)))
            for fragment in _fragments(data):
                if fragment["batch"] == bulletin["batch"] and \
                        fragment["edition"] == bulletin["edition"]:
                    terminals.update(fragment["recipients"])
            for terminal in sorted(terminals):
                db.execute(
                    "INSERT OR IGNORE INTO bulletin_audience VALUES (?,?)",
                    (bulletin["id"], terminal),
                )

        for device in data.get("device_registrations", []):
            # A device may be re-registered with changed capabilities; each
            # registration is its own immutable evidence record, while the
            # registration table keeps the current capability per terminal.
            existing = db.execute(
                "SELECT raw FROM evidence WHERE id=? AND kind='device-registration'",
                ("DEVICE-" + device["terminal"],),
            ).fetchone()
            capability = int(bool(device["supports_neutral"]))
            if existing is None:
                evidence = dict(device)
                evidence.setdefault("id", "DEVICE-" + device["terminal"])
                record(db, "device-registration", evidence)
                db.execute(
                    "INSERT OR IGNORE INTO device_registration VALUES (?,?)",
                    (device["terminal"], capability),
                )
            current = db.execute(
                "SELECT supports_neutral FROM device_registration WHERE terminal=?",
                (device["terminal"],),
            ).fetchone()
            if current is not None and current["supports_neutral"] != capability:
                import hashlib
                import json as _json

                revision = db.execute(
                    "SELECT count(*) AS n FROM evidence WHERE id LIKE ?",
                    ("DEVICE-" + device["terminal"] + "-REV%",),
                ).fetchone()["n"] + 1
                evidence = dict(device)
                evidence["id"] = f"DEVICE-{device['terminal']}-REV{revision}"
                record(db, "device-registration", evidence)
                db.execute(
                    "UPDATE device_registration SET supports_neutral=? WHERE terminal=?",
                    (capability, device["terminal"]),
                )

        for fragment in _fragments(data):
            if record(db, KIND_FRAGMENT, fragment):
                db.execute(
                    "INSERT INTO relay_fragment VALUES (?,?,?,?,?,?,?,?)",
                    (fragment["id"], fragment["batch"], fragment["edition"],
                     fragment["relay"], fragment.get("window_start"),
                     fragment.get("window_end"),
                     _encode_list(fragment["recipients"]),
                     int(fragment["received_count"])),
                )
            batches.add(fragment["batch"])

        for confirmation in data.get("terminal_confirmations", []):
            item = dict(confirmation)
            item.setdefault("id", _synthetic_id("CONF", item))
            if record(db, KIND_TERMINAL, item):
                db.execute(
                    "INSERT INTO terminal_confirmation VALUES (?,?,?,?,?,?,?)",
                    (item["id"], item["batch"], item["edition"],
                     item["terminal"], int(bool(item["confirmed"])),
                     item["witness"], item["at"]),
                )
            batches.add(item["batch"])

        for readback in data.get("board_readbacks", []):
            item = dict(readback)
            item.setdefault("id", _synthetic_id("READ", item))
            if record(db, KIND_READBACK, item):
                db.execute(
                    "INSERT INTO board_readback VALUES (?,?,?,?,?,?,?)",
                    (item["id"], item["batch"], item["terminal"],
                     item["edition"], item["board_serial"],
                     item["witness"], item["at"]),
                )
            batches.add(item["batch"])

        for authorization in data.get("correction_authorizations", []):
            item = dict(authorization)
            item.setdefault("id", _synthetic_id("AUTH", item))
            if record(db, KIND_AUTHORIZATION, item):
                db.execute(
                    "INSERT INTO correction_authorization VALUES (?,?,?,?,?,?,?)",
                    (item["id"], item["batch"], item["edition"],
                     item["kind"], item.get("subject"),
                     _encode_list(item["allowed_terminals"]),
                     item["registered_at"]),
                )
            batches.add(item["batch"])

        for command in data.get("correction_commands", []):
            item = dict(command)
            item.setdefault("command_id", _synthetic_id("CMD", item))
            batches.add(item["batch"])
            _issue_command(db, item)

        for occupation in data.get("occupation_confirmations", []):
            item = dict(occupation)
            item.setdefault("id", _synthetic_id("OCC", item))
            batches.add(item["batch"])
            _record_occupation(db, item)

        for batch in sorted(batches):
            if _editions(db, batch):
                refresh(db, batch)
                _recompute_analysis(db, batch)
    return [dict(row) for row in
            db.execute("SELECT * FROM bulletin ORDER BY id")]


# --------------------------------------------------------------------------- #
# World solving: one interpretation per terminal across every relay summary
# --------------------------------------------------------------------------- #

def _encode_list(values):
    import json
    return json.dumps(list(values), ensure_ascii=False)


def _decode_list(blob):
    import json
    return json.loads(blob)


def _reception_audience(db, batch, edition):
    """Terminals about which reception evidence actually says something.

    Only relay fragment groups and direct terminal confirmations bound the
    reception worlds: a screen declared in the bulletin audience but never
    mentioned by any fragment or direct confirmation contributes no
    reception fact and so cannot vary the solved worlds.  It still appears in
    the physical audience used for occupation and correction queries.
    """
    terminals = set()
    for row in db.execute(
        "SELECT recipients FROM relay_fragment WHERE batch=? AND edition=?",
        (batch, edition),
    ):
        terminals.update(_decode_list(row["recipients"]))
    terminals.update(r["terminal"] for r in db.execute(
        "SELECT terminal FROM terminal_confirmation WHERE batch=? AND edition=?",
        (batch, edition),
    ))
    return sorted(terminals)


def _edition_audience(db, batch, edition):
    """All terminals that could hold an edition.

    That is the declared bulletin audience plus relay/direct reception
    evidence, every terminal targeted by a correction of this edition and
    every terminal that physically occupied it; a correction edition need not
    have a separately registered bulletin row.
    """
    terminals = {r["terminal"] for r in db.execute(
        """SELECT a.terminal FROM bulletin_audience a
           JOIN bulletin b ON b.id = a.bulletin_id
           WHERE b.batch=? AND b.edition=?""",
        (batch, edition),
    )}
    terminals.update(_reception_audience(db, batch, edition))
    for row in db.execute(
        "SELECT targets FROM correction_dispatch WHERE batch=? AND edition=?",
        (batch, edition),
    ):
        terminals.update(_decode_list(row["targets"]))
    terminals.update(r["terminal"] for r in db.execute(
        "SELECT terminal FROM board_version_history WHERE batch=? AND edition=?",
        (batch, edition),
    ))
    return sorted(terminals)


def _editions(db, batch):
    editions = {r["edition"] for r in db.execute(
        "SELECT DISTINCT edition FROM bulletin WHERE batch=?", (batch,))}
    for table in ("relay_fragment", "terminal_confirmation",
                  "correction_dispatch", "board_version_history"):
        editions.update(r["edition"] for r in db.execute(
            f"SELECT DISTINCT edition FROM {table} WHERE batch=?", (batch,)))
    return sorted(editions)


def _solve_edition(db, batch, edition):
    """Return every confirmed-set compatible with the local receipts.

    The reported *compatible_worlds* describe the final reception state.  Two
    overlapping summaries in one window are constraints on a single set, so
    their counts are never added.  Fragments with distinct time windows are
    separate snapshots of the same roll-out: each window is solved on its own,
    and a final world is admissible when there is a monotone sequence of
    snapshot worlds leading to it, since reception can only grow over time.
    Direct, duty-witnessed confirmations pin the final state.
    """
    reception_audience = _reception_audience(db, batch, edition)
    fragments = db.execute(
        "SELECT * FROM relay_fragment WHERE batch=? AND edition=? ORDER BY window_start,window_end,id",
        (batch, edition),
    ).fetchall()
    direct = db.execute(
        "SELECT terminal,confirmed FROM terminal_confirmation WHERE batch=? AND edition=?",
        (batch, edition),
    ).fetchall()
    pinned_in = {r["terminal"] for r in direct if r["confirmed"]}
    pinned_out = {r["terminal"] for r in direct if not r["confirmed"]}

    def enumeration(group):
        found = []
        for mask in range(1 << len(reception_audience)):
            world = {t for i, t in enumerate(reception_audience) if mask & (1 << i)}
            if world & pinned_out:
                continue
            if all(len(world & set(_decode_list(f["recipients"]))) == f["received_count"]
                   for f in group):
                found.append(world)
        return found

    windows = []
    for fragment in fragments:
        key = (fragment["window_start"], fragment["window_end"])
        if not windows or windows[-1][0] != key:
            windows.append((key, []))
        windows[-1][1].append(fragment)

    if windows:
        snapshot_sets = [enumeration(group) for _, group in windows]
        if any(not group for group in snapshot_sets):
            # Mutually exclusive originals: no world satisfies one snapshot.
            worlds = []
        else:
            # Monotone reachability through the ordered snapshots.
            reachable = [set()]
            for index, snapshot_worlds in enumerate(snapshot_sets):
                prior = reachable[index]
                current = set()
                for world in snapshot_worlds:
                    frozen = frozenset(world)
                    if index == 0 or any(p <= world for p in prior):
                        current.add(frozen)
                reachable.append(current)
            worlds = [set(w) for w in reachable[-1] if pinned_in <= w]
    else:
        worlds = []
        for mask in range(1 << len(reception_audience)):
            world = {t for i, t in enumerate(reception_audience) if mask & (1 << i)}
            if pinned_in <= world and not (world & pinned_out):
                worlds.append(world)
    return _edition_audience(db, batch, edition), reception_audience, worlds


def _recompute_analysis(db, batch):
    db.execute("DELETE FROM reception_analysis WHERE batch=?", (batch,))
    for edition in _editions(db, batch):
        audience, reception_audience, worlds = _solve_edition(db, batch, edition)
        for terminal in audience:
            if terminal not in reception_audience:
                verdict = "no_reception_evidence"
            elif not worlds:
                verdict = "conflict"
            elif all(terminal in w for w in worlds):
                verdict = "confirmed"
            elif all(terminal not in w for w in worlds):
                verdict = "denied"
            else:
                verdict = "unknown"
            db.execute(
                "INSERT OR REPLACE INTO reception_analysis VALUES (?,?,?,?)",
                (batch, edition, terminal, verdict),
            )


def _occupation_map(db, batch):
    """Latest physically confirmed occupied edition per terminal."""
    latest = {}
    for row in db.execute(
        "SELECT terminal,edition FROM board_version_history WHERE batch=? ORDER BY at,id",
        (batch,),
    ):
        latest[row["terminal"]] = row["edition"]
    return latest


def _possible_displays(db, batch, edition, audience, worlds, occupied,
                       reception_audience):
    """Editions a screen could currently show, across all compatible worlds."""
    result = {}
    for terminal in audience:
        if terminal in occupied:
            result[terminal] = [occupied[terminal]]
            continue
        if terminal not in reception_audience:
            # No reception fact for this screen: it can only still hold the
            # previous edition, the current one cannot be shown via evidence.
            prior = _prior_edition(db, batch, edition)
            result[terminal] = [prior] if prior is not None else []
            continue
        seen = set()
        for world in worlds:
            seen.add(edition if terminal in world else _prior_edition(db, batch, edition))
        result[terminal] = sorted(e for e in seen if e is not None)
    return result


def _prior_edition(db, batch, edition):
    prior = db.execute(
        "SELECT MAX(edition) AS e FROM bulletin WHERE batch=? AND edition=?",
        (batch, edition - 1),
    ).fetchone()["e"]
    return prior


def reception_query(db, batch=None):
    """Summarize known reception, clearance state and executable corrections."""
    initialize(db)
    if batch is None:
        batches = [r["batch"] for r in db.execute(
            "SELECT DISTINCT batch FROM bulletin ORDER BY batch")]
    else:
        batches = [batch]
    return [_batch_query(db, b) for b in batches]


def _batch_query(db, batch):
    editions = _editions(db, batch)
    occupied = _occupation_map(db, batch)
    edition_reports = []
    for edition in editions:
        audience, reception_audience, worlds = _solve_edition(db, batch, edition)
        if not reception_audience:
            state = "no_reception_evidence"
        elif not worlds:
            state = "conflict"
        else:
            verdicts = {t: _verdict(worlds, t) for t in reception_audience}
            state = "unknown" if any(v == "unknown" for v in verdicts.values()) \
                else "resolved"
        terminals = []
        displays = _possible_displays(db, batch, edition, audience, worlds,
                                      occupied, reception_audience)
        for terminal in audience:
            if not worlds and terminal in reception_audience:
                reception = "conflict"
            elif terminal not in reception_audience:
                reception = "no_reception_evidence"
            else:
                reception = _verdict(worlds, terminal)
            terminals.append({
                "terminal": terminal,
                "reception": reception,
                "possible_display": displays[terminal],
                "occupied_edition": occupied.get(terminal),
                "correction": _correction_options(db, batch, edition, terminal, worlds),
                "collection_actions": _collection_actions(
                    db, batch, edition, terminal, worlds,
                    terminal in reception_audience),
            })
        edition_reports.append({
            "edition": edition,
            "state": state,
            "world_count": len(worlds),
            "compatible_worlds": sorted((sorted(w) for w in worlds),
                                        key=lambda w: (len(w), w)),
            "terminals": terminals,
        })

    latest_edition = editions[-1] if editions else None
    clearances = []
    if editions:
        all_terminals = sorted(set().union(*[
            set(_edition_audience(db, batch, e)) for e in editions
        ]))
        for old_edition in editions[:-1]:
            physical_audience = set(_edition_audience(db, batch, old_edition))
            _, reception_audience, worlds = _solve_edition(db, batch, old_edition)
            still_possible = set()
            unevidenced = set()
            for terminal in all_terminals:
                if terminal in occupied:
                    if occupied[terminal] <= old_edition:
                        still_possible.add(terminal)
                    continue
                if terminal not in physical_audience:
                    continue
                if terminal not in reception_audience:
                    # The screen could physically hold the old board but no
                    # reception fact covers it: clearance cannot be signed off.
                    unevidenced.add(terminal)
                elif any(terminal in w for w in worlds):
                    still_possible.add(terminal)
                else:
                    unevidenced.add(terminal)
            clearances.append({
                "edition": old_edition,
                "cleared": not still_possible and not unevidenced,
                "screens_still_holding": sorted(still_possible),
                "screens_without_clearance_evidence": sorted(unevidenced),
            })

    target_row = db.execute(
        "SELECT confirmation_target FROM bulletin WHERE batch=? LIMIT 1",
        (batch,),
    ).fetchone()
    return {
        "batch": batch,
        "current_target": target_row[0] if target_row else "not_assigned",
        "editions": edition_reports,
        "old_board_clearance": clearances,
        "dispatches": _dispatch_report(db, batch),
    }


def _verdict(worlds, terminal):
    if not worlds:
        return "conflict"
    if all(terminal in w for w in worlds):
        return "confirmed"
    if all(terminal not in w for w in worlds):
        return "denied"
    return "unknown"

def _collection_actions(db, batch, edition, terminal, worlds, in_reception):
    """What may be collected right now for one screen.

    A read-back of the screen's own current board is always available to that
    screen; submitting a direct confirmation is only safe when the terminal is
    actually covered by reception evidence and its reception is settled the
    same way in every compatible world.
    """
    actions = ["readback_own_board"]
    if not in_reception:
        return actions
    if worlds and all(terminal in w for w in worlds):
        actions.append("submit_confirmation:received")
    elif worlds and all(terminal not in w for w in worlds):
        actions.append("submit_confirmation:not_received")
    return actions


def _correction_options(db, batch, edition, terminal, worlds):
    device = db.execute(
        "SELECT supports_neutral FROM device_registration WHERE terminal=?",
        (terminal,),
    ).fetchone()
    neutral_auth = db.execute(
        """SELECT * FROM correction_authorization
           WHERE batch=? AND edition=? AND kind='neutral'
           ORDER BY registered_at,id""",
        (batch, edition),
    ).fetchall()
    in_neutral_group = any(
        terminal in _decode_list(a["allowed_terminals"]) for a in neutral_auth
    )
    neutral = {"path": "neutral_replacement", "allowed": False, "reasons": []}
    if not device:
        neutral["reasons"].append("device_not_registered")
    elif not device["supports_neutral"]:
        neutral["reasons"].append("plain_workstation_no_neutral_rule")
    if not in_neutral_group:
        neutral["reasons"].append("no_neutral_group_authorization")
    if not neutral["reasons"]:
        neutral["allowed"] = True
        neutral["effect"] = "stops_old_board_only"

    restricted = {"path": "restricted_correction", "allowed": False, "reasons": []}
    readback = db.execute(
        """SELECT * FROM board_readback WHERE batch=? AND terminal=?
           ORDER BY at DESC,id DESC""",
        (batch, terminal),
    ).fetchone()
    if not readback:
        restricted["reasons"].append("terminal_board_readback_required")
    else:
        restricted["readback"] = {
            "edition": readback["edition"],
            "board_serial": readback["board_serial"],
        }
        matching = [
            a for a in db.execute(
                "SELECT * FROM correction_authorization WHERE batch=? AND kind='restricted'",
                (batch,),
            ).fetchall()
            if a["edition"] == readback["edition"]
            and terminal in _decode_list(a["allowed_terminals"])
        ]
        if not matching:
            restricted["reasons"].append("no_restricted_authorization_for_this_board")
        else:
            restricted["authorization_id"] = matching[0]["id"]
            restricted["subject"] = matching[0]["subject"]
    if not restricted["reasons"]:
        restricted["allowed"] = True
        restricted["effect"] = "full_correction_content"
    return {"neutral": neutral, "restricted": restricted}


def _dispatch_report(db, batch):
    rows = db.execute(
        "SELECT * FROM correction_dispatch WHERE batch=? ORDER BY issued_at,command_id",
        (batch,),
    ).fetchall()
    report = []
    for row in rows:
        report.append({
            "command_id": row["command_id"],
            "kind": row["kind"],
            "edition": row["edition"],
            "status": row["status"],
            "targets": _decode_list(row["targets"]),
            "delivered_terminals": [r["terminal"] for r in db.execute(
                "SELECT terminal FROM terminal_delivery WHERE command_id=? AND delivered=1",
                (row["command_id"],))],
            "pending_terminals": [r["terminal"] for r in db.execute(
                "SELECT terminal FROM terminal_delivery WHERE command_id=? AND delivered=0",
                (row["command_id"],))],
        })
    return report


# --------------------------------------------------------------------------- #
# Correction procedures: authorization is registered before any result is seen
# --------------------------------------------------------------------------- #

def register_authorization(db, authorization):
    item = dict(authorization)
    item.setdefault("id", _synthetic_id("AUTH", item))
    with db:
        initialize(db)
        if record(db, KIND_AUTHORIZATION, item):
            db.execute(
                "INSERT INTO correction_authorization VALUES (?,?,?,?,?,?,?)",
                (item["id"], item["batch"], item["edition"], item["kind"],
                 item.get("subject"), _encode_list(item["allowed_terminals"]),
                 item["registered_at"]),
            )
        _recompute_analysis(db, item["batch"])
    return _authorization_dict(db, item["id"])


def _authorization_dict(db, auth_id):
    row = db.execute(
        "SELECT * FROM correction_authorization WHERE id=?", (auth_id,)).fetchone()
    out = dict(row)
    out["allowed_terminals"] = _decode_list(out["allowed_terminals"])
    return out


def _deny(db, item, code, reason):
    import datetime
    command_id = item["command_id"]
    at = item.get("issued_at") or datetime.datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT OR IGNORE INTO denied_action VALUES (?,?,?,?,?,?)",
        (command_id, item.get("batch"), item["kind"], code, reason, at),
    )
    history(db, "incident_notice", item.get("batch", ""), "denied:" + code, reason)
    return {"command_id": command_id, "status": "denied",
            "reason_code": code, "reason": reason}


def issue_correction(db, command):
    item = dict(command)
    item.setdefault("command_id", _synthetic_id("CMD", item))
    with db:
        initialize(db)
        return _issue_command(db, item)


def _issue_command(db, item):
    command_id = item["command_id"]
    if db.execute("SELECT 1 FROM correction_dispatch WHERE command_id=?",
                  (command_id,)).fetchone():
        # Retransmission of the same command preserves the original fact; it
        # never creates a second delivery.
        return {"command_id": command_id, "status": "duplicate_retransmission"}
    if db.execute("SELECT 1 FROM denied_action WHERE command_id=?",
                  (command_id,)).fetchone():
        row = db.execute("SELECT * FROM denied_action WHERE command_id=?",
                         (command_id,)).fetchone()
        return {"command_id": command_id, "status": "denied",
                "reason_code": row["reason_code"], "reason": row["reason"]}

    kind = item["kind"]
    batch = item["batch"]
    edition = item["edition"]
    targets = list(item.get("targets", []))
    if kind == "neutral":
        if item.get("restricted_batch_ref"):
            return _deny(db, item, "neutral_card_must_be_batch_free",
                         "pre-authorized neutral replacement card must not contain a restricted batch number")
        device_missing = [t for t in targets if not db.execute(
            "SELECT 1 FROM device_registration WHERE terminal=? AND supports_neutral=1",
            (t,)).fetchone()]
        if device_missing:
            return _deny(db, item, "unsupported_workstation",
                         "workstation has no neutral update rule: " + ",".join(sorted(device_missing)))
        auth = db.execute(
            """SELECT * FROM correction_authorization
               WHERE batch=? AND edition=? AND kind='neutral'
               ORDER BY registered_at,id""",
            (batch, edition),
        ).fetchall()
        auth = [a for a in auth
                if set(targets) <= set(_decode_list(a["allowed_terminals"]))
                and a["registered_at"] <= item["issued_at"]]
        if not auth:
            return _deny(db, item, "no_pre_registered_neutral_authorization",
                         "neutral group authorization covering every target must be registered before issuance")
        auth_id = auth[0]["id"]
        # The neutral card may be pushed to the whole authorized group without
        # identifying who currently holds the old board.
        db.execute(
            """INSERT INTO correction_dispatch
               (command_id,batch,edition,kind,authorization_id,old_edition,
                board_serial,subject,targets,restricted_batch_ref,issued_at,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (command_id, batch, edition, "neutral", auth_id, None, None,
             None, _encode_list(targets), None, item["issued_at"], "issued"),
        )
        _record_command(db, item)
        for terminal in targets:
            db.execute("INSERT INTO terminal_delivery VALUES (?,?,0)",
                       (command_id, terminal))
        history(db, "incident_notice", batch, "neutral_issued", command_id)
        return {"command_id": command_id, "status": "issued",
                "kind": "neutral", "targets": targets,
                "note": "submission is not delivery; awaiting device occupation confirmation"}

    if kind == "restricted":
        old_edition = item.get("old_edition")
        serial = item.get("board_serial")
        if old_edition is None or not serial:
            return _deny(db, item, "restricted_requires_named_board",
                         "restricted correction must name the workstation and the actual old board serial")
        # Role authorization is edition-specific: once the bulletin target moves
        # to another console, the old party cannot send a new restricted
        # correction even though its old neutral clearance stays valid.
        active_target = current_target(db, batch)
        if item.get("subject") and item["subject"] != active_target:
            return _deny(db, item, "subject_no_longer_current_target",
                         f"subject {item['subject']} is not the current confirmation target {active_target}")
        blocked = []
        auth_rows = db.execute(
            "SELECT * FROM correction_authorization WHERE batch=? AND kind='restricted'",
            (batch,),
        ).fetchall()
        for terminal in targets:
            readback = db.execute(
                """SELECT * FROM board_readback WHERE batch=? AND terminal=?
                   ORDER BY at DESC,id DESC""",
                (batch, terminal),
            ).fetchone()
            if not readback:
                blocked.append((terminal, "terminal_board_readback_required"))
                continue
            if readback["edition"] != old_edition or readback["board_serial"] != serial:
                blocked.append((terminal, "readback_does_not_match_named_board"))
                continue
            if not db.execute(
                """SELECT 1 FROM bulletin_audience a JOIN bulletin b ON b.id=a.bulletin_id
                   WHERE b.batch=? AND b.edition=? AND a.terminal=?""",
                (batch, old_edition, terminal),
            ).fetchone():
                blocked.append((terminal, "terminal_not_in_old_edition_audience"))
                continue
            matching = [a for a in auth_rows
                        if a["edition"] == old_edition
                        and terminal in _decode_list(a["allowed_terminals"])
                        and (not a["subject"] or a["subject"] == item.get("subject"))
                        and a["registered_at"] <= item["issued_at"]]
            if not matching:
                blocked.append((terminal, "no_restricted_authorization_for_this_board"))
        if blocked:
            return _deny(db, item, blocked[0][1],
                         "restricted correction blocked: "
                         + ";".join(t + ":" + code for t, code in blocked))
        auth = [a for a in auth_rows
                if a["edition"] == old_edition
                and set(targets) <= set(_decode_list(a["allowed_terminals"]))
                and (not a["subject"] or a["subject"] == item.get("subject"))][0]
        db.execute(
            """INSERT INTO correction_dispatch
               (command_id,batch,edition,kind,authorization_id,old_edition,
                board_serial,subject,targets,restricted_batch_ref,issued_at,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (command_id, batch, edition, "restricted", auth["id"], old_edition,
             serial, item.get("subject"), _encode_list(targets),
             item.get("restricted_batch_ref"), item["issued_at"], "issued"),
        )
        _record_command(db, item)
        for terminal in targets:
            db.execute("INSERT INTO terminal_delivery VALUES (?,?,0)",
                       (command_id, terminal))
        history(db, "incident_notice", batch, "restricted_issued", command_id)
        return {"command_id": command_id, "status": "issued",
                "kind": "restricted", "targets": targets,
                "note": "submission is not delivery; awaiting device occupation confirmation"}

    return _deny(db, item, "unknown_correction_kind", kind)


def record_occupation(db, occupation):
    item = dict(occupation)
    item.setdefault("id", _synthetic_id("OCC", item))
    with db:
        initialize(db)
        result = _record_occupation(db, item)
        _recompute_analysis(db, item["batch"])
        return result


def _record_occupation(db, item):
    if db.execute("SELECT 1 FROM board_version_history WHERE id=?",
                  (item["id"],)).fetchone():
        return {"id": item["id"], "status": "duplicate_retransmission"}
    batch, terminal, edition = item["batch"], item["terminal"], item["edition"]
    last = db.execute(
        "SELECT edition FROM board_version_history WHERE batch=? AND terminal=? ORDER BY at DESC,id DESC",
        (batch, terminal),
    ).fetchone()
    if last and edition < last["edition"]:
        # A device cannot confirm a downgrade on the same screen.
        raise ValueError("occupation version regresses for terminal " + terminal)
    eligible = set(_edition_audience(db, batch, edition))
    if item.get("command_id"):
        target_row = db.execute(
            "SELECT targets FROM correction_dispatch WHERE command_id=?",
            (item["command_id"],),
        ).fetchone()
        if target_row:
            eligible.update(_decode_list(target_row["targets"]))
    if terminal not in eligible:
        raise ValueError("terminal not in audience for the confirmed edition")

    is_new = record(db, KIND_OCCUPATION, item)
    if is_new:
        db.execute(
            "INSERT INTO board_version_history VALUES (?,?,?,?,?,?)",
            (item["id"], batch, terminal, edition,
             item.get("command_id"), item["at"]),
        )
        # A device confirmation turns a pending command into actual delivery,
        # but only for the screen that physically accepted the new board.
        if item.get("command_id"):
            db.execute(
                "UPDATE terminal_delivery SET delivered=1 WHERE command_id=? AND terminal=?",
                (item["command_id"], terminal),
            )
            pending = db.execute(
                "SELECT count(*) AS n FROM terminal_delivery WHERE command_id=? AND delivered=0",
                (item["command_id"],)).fetchone()["n"]
            status = "partially_confirmed" if pending else "confirmed"
            db.execute(
                "UPDATE correction_dispatch SET status=? WHERE command_id=?",
                (status, item["command_id"]),
            )
        history(db, "incident_notice", batch,
                "occupied_edition_v" + str(edition), terminal)
    return {"id": item["id"], "status": "confirmed" if is_new else "duplicate_retransmission",
            "terminal": terminal, "edition": edition}


def record_readback(db, readback):
    item = dict(readback)
    item.setdefault("id", _synthetic_id("READ", item))
    with db:
        initialize(db)
        if record(db, KIND_READBACK, item):
            db.execute(
                "INSERT INTO board_readback VALUES (?,?,?,?,?,?,?)",
                (item["id"], item["batch"], item["terminal"], item["edition"],
                 item["board_serial"], item["witness"], item["at"]),
            )
        _recompute_analysis(db, item["batch"])
        return dict(db.execute(
            "SELECT * FROM board_readback WHERE id=?", (item["id"],)).fetchone())
