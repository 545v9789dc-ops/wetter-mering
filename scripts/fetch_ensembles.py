#!/usr/bin/env python3
"""Open-Meteo GFS/IFS ensemble collector for Mering."""
import json, math, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

MODELS = {"GFS-ENS": "ncep_gefs025", "IFS-ENS": "ecmwf_ifs025_ensemble"}
FIELDS = ("temperature_2m", "precipitation", "snowfall")
TZ = ZoneInfo("Europe/Berlin")
OUT = Path("data/mering_ensembles.json")

def fetch(model):
    query = urlencode(dict(latitude=48.2656, longitude=10.9846, models=model,
                           hourly=",".join(FIELDS), timezone="GMT",
                           forecast_days=15, precipitation_unit="mm"))
    url = "https://ensemble-api.open-meteo.com/v1/ensemble?" + query
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent":"wetter-mering/4.1"}), timeout=25) as r:
                return json.load(r)
        except HTTPError as e:
            if e.code not in (429,500,502,503,504) or attempt==2: raise
        except (URLError, TimeoutError, OSError):
            if attempt==2: raise
        time.sleep(2**attempt)

def valid(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def summarize(obj):
    h=obj["hourly"]
    times=[datetime.fromisoformat(t).replace(tzinfo=timezone.utc).astimezone(TZ).date().isoformat()
           for t in h["time"]]
    members={}
    for key,values in h.items():
        for field in FIELDS:
            prefix=field+"_member"
            if key.startswith(prefix) and isinstance(values,list) and len(values)==len(times):
                members.setdefault(key[len(prefix):],{})[field]=values
    if not members: raise ValueError("No ensemble member arrays found")
    daily=[]
    for day in sorted(set(times)):
        indices=[i for i,d in enumerate(times) if d==day]
        row={"date":day,"probabilities":{},"available_members":{},
             "data_quality":{"expected_hours":len(indices),"fields":{}}}
        for label,field,predicate in (
            ("frost_any_hour","temperature_2m",lambda v:min(v)<0),
            ("precipitation_ge_1mm","precipitation",lambda v:sum(v)>=1),
            ("snowfall_positive","snowfall",lambda v:sum(v)>0)):
            outcomes=[]
            complete=partial=empty=missing_field=0
            valid_hour_counts=[]
            for m in members.values():
                vals=m.get(field)
                if vals is None:
                    missing_field+=1
                    continue
                segment=[vals[i] for i in indices]
                nvalid=sum(valid(v) for v in segment)
                valid_hour_counts.append(nvalid)
                if nvalid==len(indices):
                    complete+=1
                    outcomes.append(predicate(segment))
                elif nvalid==0:
                    empty+=1
                else:
                    partial+=1
            row["data_quality"]["fields"][field]={
                "complete_members":complete,
                "partial_members":partial,
                "empty_members":empty,
                "missing_field_members":missing_field,
                "minimum_valid_hours":min(valid_hour_counts) if valid_hour_counts else None,
                "maximum_valid_hours":max(valid_hour_counts) if valid_hour_counts else None
            }
            row["available_members"][label]=len(outcomes)
            row["probabilities"][label]=round(sum(outcomes)/len(outcomes),3) if outcomes else None
        daily.append(row)
    return {"member_count":len(members),"daily":daily,
            "quality_note":"Only members with every expected hourly value are included in daily event fractions."}

def main():
    result={"retrieved_at_utc":datetime.now(timezone.utc).isoformat(),
            "source":"Open-Meteo Ensemble API","models":{},"errors":{},
            "note":"Member fractions are model-based estimates, not calibrated probabilities."}
    for label,model in MODELS.items():
        try: result["models"][label]=summarize(fetch(model))
        except (HTTPError,URLError,TimeoutError,OSError,ValueError,KeyError,TypeError) as e:
            result["errors"][label]=str(e)[:250]
    if not result["models"]: raise SystemExit("No valid ensembles; existing output preserved")
    OUT.parent.mkdir(parents=True,exist_ok=True)
    tmp=OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    tmp.replace(OUT)

if __name__=="__main__": main()
