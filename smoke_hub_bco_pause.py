"""Read-only live BCO-to-Hub pause-state consistency check."""
import json
import time
from urllib.request import Request, urlopen
from hub_bco_pause_contract import verify_bco_pause

def read(url):
    with urlopen(Request(url,method="GET",headers={"Accept":"application/json",
                                                   "User-Agent":"BCO-Pause-Status-ReadOnly"}),timeout=18) as resp:
        if resp.status!=200: raise AssertionError(f"{url}: HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))

def check():
    bco=verify_bco_pause(read("https://bco-live-production.up.railway.app/bco-live-entry-pause-v2"))
    hub=read("https://project-exit-plan-portfolio-hub-production.up.railway.app/api/aggregate")
    source=(((hub.get("sources") or {}).get("bco") or {}).get("data") or {})
    badge=source.get("entry_pause") or {}
    if badge.get("ok") is not True or badge.get("paused") is not bco["paused"]:
        raise AssertionError("Hub BCO entry-pause field missing or differs from live BCO")
    print("PASS: Hub BCO new-entry pause matches production BCO, paused="+str(bco["paused"]))

if __name__=="__main__":
    err=""
    for n in range(18):
        try:
            check()
            break
        except Exception as e:
            err=f"{type(e).__name__}: {e}"
            print(f"Attempt {n+1}/18: "+err)
            time.sleep(5)
    else:
        raise SystemExit("FAIL: "+err)
