import sqlite3
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS file_journal(id INTEGER PRIMARY KEY,command TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS waste_profile(id TEXT PRIMARY KEY,batch TEXT NOT NULL,version INTEGER NOT NULL,code TEXT NOT NULL,hazard TEXT NOT NULL,container_class TEXT NOT NULL,created_at TEXT NOT NULL,storage_until TEXT NOT NULL,issued_by TEXT NOT NULL,withdrawn INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS waste_batch(id TEXT PRIMARY KEY,owner TEXT NOT NULL,declared_kg REAL NOT NULL,container_class TEXT NOT NULL,classification_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS classification(batch TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,profile_id TEXT,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS vehicle_permit(id TEXT PRIMARY KEY,vehicle TEXT NOT NULL,driver TEXT NOT NULL,code TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,route TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manifest(id TEXT PRIMARY KEY,batch TEXT NOT NULL,vehicle TEXT NOT NULL,permit_id TEXT NOT NULL,route TEXT NOT NULL,depart_at TEXT NOT NULL,arrive_at TEXT NOT NULL,out_kg REAL NOT NULL,producer_sign TEXT,carrier_sign TEXT);
CREATE TABLE IF NOT EXISTS handoff(id TEXT PRIMARY KEY,manifest_id TEXT NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,signer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transport(batch TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,manifest_id TEXT,liable_party TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS receiver_permit(id TEXT PRIMARY KEY,receiver TEXT NOT NULL,code TEXT NOT NULL,method TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS weigh_in(id TEXT PRIMARY KEY,batch TEXT NOT NULL,receiver_id TEXT NOT NULL,permit_id TEXT NOT NULL,at TEXT NOT NULL,net_kg REAL NOT NULL,scale_witness TEXT NOT NULL,kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS closure(batch TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,weight_delta REAL,liable_party TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS investigation(id TEXT PRIMARY KEY,batch TEXT NOT NULL,at TEXT NOT NULL,source TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS future_dispatch_hold(batch TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,closure_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision_history(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,batch TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,source_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def history(db,subsystem,batch,state,reason,source_id=None):
    db.execute('INSERT INTO decision_history(subsystem,batch,state,reason,source_id) VALUES(?,?,?,?,?)',(subsystem,batch,state,reason,source_id))
