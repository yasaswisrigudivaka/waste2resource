"""Waste2Resource backend. Run: pip install -r requirements.txt ; python app.py  (frontend = index.html)"""
import os, json, sqlite3, uuid, random, math, base64, datetime
from flask import Flask, request, jsonify, send_from_directory, Response
try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass

B = os.path.dirname(os.path.abspath(__file__)); UP = os.path.join(B, 'uploads'); DB = os.path.join(B, 'w2r.db')
if os.environ.get('VERCEL'):  # Vercel only allows writing to /tmp (data there is temporary)
    UP = '/tmp/uploads'; DB = '/tmp/w2r.db'
os.makedirs(UP, exist_ok=True)
app = Flask(__name__); app.config['MAX_CONTENT_LENGTH'] = 6 * 1024 * 1024
KEY = os.getenv('AUTHORITY_KEY', '')
LAT = float(os.getenv('MAP_CENTER_LAT', '16.3067')); LNG = float(os.getenv('MAP_CENTER_LNG', '80.4365'))
FLOW = ['REPORTED', 'VERIFIED', 'ASSIGNED', 'COLLECTION', 'PROCESSING', 'RESOLVED']
PATHWAY = {'Plastic': 'Sorting + authorized recycling / appropriate energy recovery', 'Paper/Cardboard': 'Paper recycling',
 'Glass': 'Glass recovery', 'Metal': 'Material recovery / recycling', 'Organic': 'Composting / anaerobic digestion / biogas',
 'E-waste': 'Authorized e-waste recycling', 'Hazardous': 'Specialized authorized treatment',
 'Construction': 'Construction-material recovery', 'Residual': 'Appropriate treatment / energy recovery where suitable'}
CATS = list(PATHWAY)

# ---------------- AI layer (only place that knows the provider) ----------------
def mock_ai(data):
    r = random.Random(len(data)); picks = r.sample(CATS, r.choice([1, 2, 3])); w = [r.randint(20, 70) for _ in picks]
    comp = {c: round(100 * x / sum(w)) for c, x in zip(picks, w)}; comp[picks[0]] += 100 - sum(comp.values())
    return {'composition': comp, 'quantity_kg': r.randint(5, 120), 'confidence': round(r.uniform(.62, .91), 2), 'source': 'mock (simulated)'}

def bedrock_ai(data, mime):
    import boto3
    c = boto3.client('bedrock-runtime', region_name=os.getenv('AWS_REGION', 'us-east-1'))
    p = 'Classify the waste in this photo. Reply ONLY JSON: {"composition":{"<category>":percent},"quantity_kg":number,"confidence":0-1}. Categories: ' + ', '.join(CATS) + '. Percentages sum to 100.'
    fmt = {'image/jpeg': 'jpeg', 'image/png': 'png', 'image/webp': 'webp'}[mime]
    o = c.converse(modelId=os.getenv('BEDROCK_MODEL_ID', ''), messages=[{'role': 'user', 'content': [{'image': {'format': fmt, 'source': {'bytes': data}}}, {'text': p}]}])
    t = o['output']['message']['content'][0]['text']; j = json.loads(t[t.index('{'):t.rindex('}') + 1]); j['source'] = 'bedrock (model estimate)'
    return j

def ai_analyze(data, mime):
    try:
        res = bedrock_ai(data, mime) if os.getenv('AI_PROVIDER', 'mock') == 'bedrock' else mock_ai(data)
    except Exception:
        res = mock_ai(data)
    res['severity_score'] = severity(res['composition'], res.get('quantity_kg', 20))
    res['disclaimer'] = 'AI estimation, not a verified measurement'
    return res

def severity(comp, qty, near_sensitive=False, repeats=0):
    s = min(40, qty / 3) + comp.get('Hazardous', 0) * .5 + comp.get('E-waste', 0) * .3 + (15 if near_sensitive else 0) + min(15, repeats * 5)
    return int(max(0, min(100, s)))

def sniff(d):
    if d[:3] == b'\xff\xd8\xff': return 'image/jpeg', '.jpg'
    if d[:8] == b'\x89PNG\r\n\x1a\n': return 'image/png', '.png'
    if d[:4] == b'RIFF' and d[8:12] == b'WEBP': return 'image/webp', '.webp'
    return None, None

# ---------------- DB ----------------
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init():
    c = db()
    c.executescript("""CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,name TEXT,email TEXT,phone TEXT,role TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS reports(report_id TEXT PRIMARY KEY,user_id TEXT,image_url TEXT,latitude REAL,longitude REAL,address TEXT,created_at TEXT,
      category TEXT,composition TEXT,severity_score INTEGER,quantity_kg REAL,status TEXT,assigned_authority TEXT,notes TEXT,after_url TEXT,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS processing(processing_id TEXT PRIMARY KEY,report_id TEXT,company_id TEXT,processing_method TEXT,material_recovered REAL,energy_generated REAL,processing_status TEXT,completed_at TEXT);""")
    if not c.execute('SELECT 1 FROM reports').fetchone():
        seed = [('Near Govt School, Brodipet', 0.004, .002, {'Plastic': 50, 'Organic': 50}, 40, 'REPORTED'), ('Canal bank, Arundelpet', -.006, .005, {'Organic': 80, 'Residual': 20}, 90, 'VERIFIED'),
         ('Hospital road', .008, -.004, {'Hazardous': 40, 'Plastic': 60}, 25, 'ASSIGNED'), ('Market yard', -.003, -.008, {'Organic': 70, 'Paper/Cardboard': 30}, 150, 'COLLECTION'),
         ('Ring road junction', .012, .009, {'Construction': 100}, 300, 'PROCESSING'), ('Bus stand', -.01, .001, {'Plastic': 60, 'Glass': 40}, 30, 'RESOLVED'),
         ('Lake front', .002, -.012, {'E-waste': 70, 'Metal': 30}, 15, 'REPORTED'), ('Brodipet 2nd line', .005, .004, {'Organic': 60, 'Plastic': 40}, 60, 'RESOLVED')]
        for i, (a, dl, dn, comp, q, st) in enumerate(seed):
            when = (datetime.datetime.now() - datetime.timedelta(days=i * 2 + 1)).isoformat(timespec='minutes')
            c.execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('W2R-%05d' % (10001 + i), 'demo', None, LAT + dl, LNG + dn, a, when, max(comp, key=comp.get), json.dumps(comp),
                      severity(comp, q, i == 0 or i == 2), q, st, 'Ward team %d' % (i % 3 + 1) if FLOW.index(st) >= 2 else None, '', None, when))
        c.commit()
    c.close()

def row(r):
    d = dict(r); d['composition'] = json.loads(d['composition'] or '{}'); d['pathway'] = PATHWAY.get(d['category'], PATHWAY['Residual']); return d

def need_auth():
    if KEY and request.headers.get('X-Authority-Key') != KEY:
        return jsonify(error='Authority key required'), 401

def save_image(f):
    d = f.read(); mime, ext = sniff(d)
    if not mime: return None, None, None
    n = uuid.uuid4().hex + ext
    open(os.path.join(UP, n), 'wb').write(d); return n, d, mime

# ---------------- API ----------------
@app.route('/uploads/<path:n>')
def uploads(n): return send_from_directory(UP, n)

@app.route('/api/reports')
def reports():
    c = db(); r = [row(x) for x in c.execute('SELECT * FROM reports ORDER BY created_at DESC')]; c.close(); return jsonify(r)

@app.route('/api/config')
def cfg(): return jsonify(lat=LAT, lng=LNG, flow=FLOW, auth_required=bool(KEY), provider=os.getenv('AI_PROVIDER', 'mock'))

@app.route('/api/analyze', methods=['POST'])
def analyze():
    f = request.files.get('image')
    if not f: return jsonify(error='image required'), 400
    d = f.read(); mime, _ = sniff(d)
    if not mime: return jsonify(error='Invalid image (JPG/PNG/WebP only)'), 400
    return jsonify(ai_analyze(d, mime))

@app.route('/api/reports', methods=['POST'])
def create():
    f = request.files.get('image')
    if not f: return jsonify(error='image required'), 400
    n, d, mime = save_image(f)
    if not n: return jsonify(error='Invalid image (JPG/PNG/WebP only)'), 400
    try:
        lat = float(request.form['lat']); lng = float(request.form['lng'])
    except Exception:
        return jsonify(error='valid lat/lng required'), 400
    try:
        comp = json.loads(request.form.get('comp', '')); assert comp
    except Exception:
        comp = ai_analyze(d, mime)['composition']  # production: always re-classify server-side
    qty = float(request.form.get('qty', 20) or 20); c = db()
    near = c.execute('SELECT COUNT(*) FROM reports WHERE abs(latitude-?)<.003 AND abs(longitude-?)<.003', (lat, lng)).fetchone()[0]
    score = severity(comp, qty, request.form.get('sensitive') == '1', near)
    rid = 'W2R-%05d' % random.randint(20000, 99999); now = datetime.datetime.now().isoformat(timespec='minutes')
    c.execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (rid, 'citizen', '/uploads/' + n, lat, lng, request.form.get('address', '')[:200], now, max(comp, key=comp.get), json.dumps(comp), score, qty, 'REPORTED', None, request.form.get('desc', '')[:300], None, now))
    c.commit(); r = row(c.execute('SELECT * FROM reports WHERE report_id=?', (rid,)).fetchone()); c.close(); return jsonify(r), 201

@app.route('/api/reports/<rid>/status', methods=['PUT'])
def advance(rid):
    e = need_auth()
    if e: return e
    c = db(); r = c.execute('SELECT * FROM reports WHERE report_id=?', (rid,)).fetchone()
    if not r: return jsonify(error='not found'), 404
    i = FLOW.index(r['status'])
    if i == len(FLOW) - 1: return jsonify(error='already resolved'), 400
    nxt = FLOW[i + 1]; body = request.get_json(silent=True) or {}
    if body.get('to') and body['to'] != nxt: return jsonify(error='Workflow advances exactly one step: next is ' + nxt), 400
    team = body.get('team') or r['assigned_authority']
    if nxt == 'ASSIGNED' and not team: team = 'Ward team 1'
    if nxt == 'RESOLVED' and not r['after_url']: return jsonify(error='Upload an after photo before resolving'), 400
    c.execute('UPDATE reports SET status=?,assigned_authority=?,notes=?,updated_at=? WHERE report_id=?', (nxt, team, (r['notes'] or '') + (' | ' + body['note'][:200] if body.get('note') else ''), datetime.datetime.now().isoformat(timespec='minutes'), rid))
    c.commit(); out = row(c.execute('SELECT * FROM reports WHERE report_id=?', (rid,)).fetchone()); c.close(); return jsonify(out)

@app.route('/api/reports/<rid>/after', methods=['POST'])
def after(rid):
    e = need_auth()
    if e: return e
    c = db(); r = c.execute('SELECT * FROM reports WHERE report_id=?', (rid,)).fetchone()
    if not r: return jsonify(error='not found'), 404
    if r['status'] not in ('PROCESSING', 'RESOLVED'): return jsonify(error='After photo allowed once report reaches PROCESSING (needed to resolve)'), 400
    f = request.files.get('image')
    if not f: return jsonify(error='image required'), 400
    n, _, _ = save_image(f)
    if not n: return jsonify(error='Invalid image'), 400
    c.execute('UPDATE reports SET after_url=? WHERE report_id=?', ('/uploads/' + n, rid)); c.commit()
    out = row(c.execute('SELECT * FROM reports WHERE report_id=?', (rid,)).fetchone()); c.close(); return jsonify(out)

@app.route('/api/reports/<rid>/accept', methods=['POST'])
def accept(rid):
    e = need_auth()
    if e: return e
    c = db(); r = c.execute('SELECT * FROM reports WHERE report_id=?', (rid,)).fetchone()
    if not r: return jsonify(error='not found'), 404
    comp = json.loads(r['composition']); q = r['quantity_kg'] or 0; org = q * comp.get('Organic', 0) / 100
    rec = q * sum(v for k, v in comp.items() if k in ('Plastic', 'Paper/Cardboard', 'Glass', 'Metal', 'E-waste', 'Construction')) / 100
    biogas = org * 0.3; kwh = biogas * 2.0  # illustrative placeholder coefficients
    pid = 'P-' + uuid.uuid4().hex[:6]; company = (request.get_json(silent=True) or {}).get('company', 'GreenLoop Recyclers')
    c.execute('INSERT INTO processing VALUES(?,?,?,?,?,?,?,?)', (pid, rid, company, r['category'] and PATHWAY[r['category']], round(rec, 1), round(kwh, 1), 'ACCEPTED', None)); c.commit(); c.close()
    return jsonify(processing_id=pid, material_recovered_kg=round(rec, 1), energy_kwh=round(kwh, 1), label='Estimated / model-based values')

@app.route('/api/processing')
def plog():
    c = db(); r = [dict(x) for x in c.execute('SELECT * FROM processing ORDER BY rowid DESC')]; c.close(); return jsonify(r)

@app.errorhandler(413)
def big(e): return jsonify(error='File too large (max 5 MB)'), 413

@app.route('/')
def home(): return send_from_directory(B, 'index.html')



init()
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False)
