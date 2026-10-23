import json
import urllib.request

with urllib.request.urlopen(
    "http://localhost:19090/api/v1/query?query=up", timeout=10
) as r:
    result = json.load(r)
print(json.dumps({"source": "prometheus", "query": "up", "result": result}))
