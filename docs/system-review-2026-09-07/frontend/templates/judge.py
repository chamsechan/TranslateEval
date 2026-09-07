from http.server import HTTPServer,BaseHTTPRequestHandler
import json
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  body=json.dumps({'object':'list','data':[{'id':'audit-judge'}]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))));p=Path('__REVIEW_ROOT__/judge-requests.jsonl');p.open('a').write(json.dumps(data)+'\n');body=json.dumps({'choices':[{'message':{'content':json.dumps({'score':9,'reason':'UI audit local judge'})},'finish_reason':'stop'}]}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
HTTPServer(('127.0.0.1',__JUDGE_PORT__),Handler).serve_forever()
