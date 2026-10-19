"""A local webhook inbox. No third-party delivery or credentials required."""
import json,sqlite3,time
from http.server import BaseHTTPRequestHandler,HTTPServer
DB='/data/events.sqlite3'
def connect():
    db=sqlite3.connect(DB,timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, received_at TEXT, payload TEXT)')
    return db
class Handler(BaseHTTPRequestHandler):
    def reply(self,status,value):
        raw=json.dumps(value).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/healthz':
            with connect() as db:db.execute('SELECT 1')
            return self.reply(200,{'status':'ready'})
        if self.path!='/api/events':return self.reply(404,{'error':'not found'})
        with connect() as db:
            rows=db.execute('SELECT id,received_at,payload FROM events ORDER BY id DESC LIMIT 500').fetchall()
        self.reply(200,{'events':[{'id':i,'received_at':t,'payload':json.loads(p)} for i,t,p in reversed(rows)]})
    def do_POST(self):
        if self.path not in ['/hooks/application','/hooks/infrastructure']:return self.reply(404,{'error':'not found'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=262144:return self.reply(413,{'error':'body limit is 256 KiB'})
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict) or data.get('status') not in ['firing','resolved'] or not isinstance(data.get('alerts'),list):raise ValueError()
        except (ValueError,TypeError):return self.reply(400,{'error':'invalid Alertmanager payload'})
        with connect() as db:
            db.execute('INSERT INTO events(received_at,payload) VALUES(?,?)',(time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),json.dumps(data)))
            # Keep a bounded inbox; it is an educational receiver, not an audit archive.
            db.execute('DELETE FROM events WHERE id <= (SELECT coalesce(max(id),0)-500 FROM events)')
        print(json.dumps({'status':data['status'],'receiver':data.get('receiver'),'alerts':len(data['alerts'])}),flush=True)
        self.reply(200,{'stored':True})
connect().close()
HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
