"""Read-only evidence for a uniquely marked message entering its pinned host turn."""
import json
from pathlib import Path
import socket
import sqlite3
import yaml


def receipt_matches(row, binding, pin, message_id):
    receipt = json.loads(row['receipt'])
    return (row['status'] == 'received' and row['session_id'] == pin['session_id']
            and row['platform'] == pin['platform']
            and receipt.get('binding_id') == binding['id']
            and all(receipt.get(k) == binding[k] for k in
                    ('employee_id', 'project_id', 'host_kind', 'host_session_id'))
            and message_id in receipt.get('message_ids', []))


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def probe(profile, message_id, marker):
    if not marker or not message_id.startswith('message_'):
        raise ValueError('exact_message_and_marker_required')
    entry = yaml.safe_load((profile/'config.yaml').read_bytes())['plugins']['entries']['agent-mailbox-host']
    settings = entry['settings']
    home = Path(settings['home'])
    db = readonly(home/'workbench/state.db')
    try:
        message = db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone()
        if message is None or marker not in message['title'] or marker not in message['body']:
            raise ValueError('unique_marker_mismatch')
        bindings = json.loads((home/'wake/host-bindings.json').read_text())['bindings']
        matches = [b for b in bindings if b['host_kind']=='hermes'
                   and b['employee_id']==message['recipient_id'] and b['project_id']==message['project_id']]
        if len(matches) != 1:
            raise ValueError('exact_hermes_binding_required')
        b = matches[0]
        notification = db.execute('SELECT status,attempts FROM notifications WHERE state_hash=? AND target_id=?',
                                  (message_id, b['employee_id'])).fetchall()
        pending = db.execute("SELECT count(*) FROM notifications WHERE target_id=? AND status='pending'",
                             (b['employee_id'],)).fetchone()[0]
    finally:
        db.close()
    pin = settings['sessions'][b['host_session_id']]
    journal = readonly(Path(settings['state_dir'])/'deliveries.sqlite3')
    try:
        receipts = [r['id'] for r in journal.execute('SELECT * FROM deliveries')
                    if receipt_matches(r, b, pin, message_id)]
    finally:
        journal.close()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(4)
        s.connect(settings['socket_path'])
        s.sendall((json.dumps({'operation':'probe','binding':b})+'\n').encode())
        raw = b''
        while b'\n' not in raw:
            part = s.recv(8192)
            if not part: break
            raw += part
        health = json.loads(raw)
    confirmed = (entry['allow_gateway_injection'] is True
                 and health.get('route_available') is True
                 and len(notification)==1
                 and notification[0]['status'] in ('host_received','recipient_opened')
                 and bool(receipts))
    return {'passed':confirmed, 'message_id':message_id, 'marker':marker,
            'enabled':entry['allow_gateway_injection'],
            'route_available':health.get('route_available'),
            'notification':[dict(r) for r in notification],
            'original_pinned_session_turn_received':bool(receipts),
            'delivery_ids':receipts, 'pending':pending,
            'task_accepted':False, 'human_accepted':False}
