#!/usr/bin/env python3
import csv,json,math,re,xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib import robotparser
import extruct,requests

ROOT=Path(__file__).resolve().parents[2]
CITY_FILE=ROOT/"data/location/usa/city_master.csv"
FOURSQUARE_ROOT=ROOT/"data/venues/raw/foursquare"
RAW_ROOT=ROOT/"data/events/raw/schema_org"
TIMEOUT=20
SITEMAP_MAX=20
SITEMAP_URL_MAX=10000
EVENT_URL_MAX=250
SITEMAP_WORKERS=8
PAGE_WORKERS=12
FALLBACK_MILES=40.0
UA="events-schema-org-collector/1.0"
EVENT_TYPES={"Event","BusinessEvent","ChildrensEvent","ComedyEvent","ConferenceEvent","CourseInstance","DanceEvent","DeliveryEvent","EducationEvent","ExhibitionEvent","Festival","FoodEvent","Hackathon","LiteraryEvent","MusicEvent","PerformingArtsEvent","PublicationEvent","SaleEvent","ScreeningEvent","SocialEvent","SportsEvent","TheaterEvent","VisualArtsEvent","EventSeries"}
PATH_HINTS=("event","events","calendar","concert","concerts","show","shows","performance","performances","festival","festivals","comedy","schedule","program","programs","tickets")
SITEMAP_HINTS=("event","events","calendar","tribe","schedule","program")
RADIO_MARKERS=("radio show","radio program","radio hour","on-air","on air","broadcast live","live broadcast")

def txt(v):
    if v is None:return ""
    if isinstance(v,str):return re.sub(r"\s+"," ",v.strip())
    if isinstance(v,bool):return str(v).lower()
    if isinstance(v,(int,float)):return str(v)
    if isinstance(v,dict):
        for k in ("name","value","text","@id","url"):
            if k in v:
                x=txt(v[k])
                if x:return x
    if isinstance(v,list):
        return " | ".join(dict.fromkeys(x for x in (txt(i) for i in v) if x))
    return re.sub(r"\s+"," ",str(v).strip())

def host(v):
    if "://" not in str(v):v="https://"+str(v)
    try:h=urlparse(v).hostname or ""
    except ValueError:return ""
    return h[4:] if h.startswith("www.") else h

def origin(v):
    if "://" not in str(v):v="https://"+str(v)
    try:p=urlparse(v)
    except ValueError:return ""
    return f"{p.scheme}://{p.netloc}" if p.scheme in ("http","https") and p.netloc else ""

def same_host(a,b):return host(a)==host(b)

def req(url):
    r=requests.get(url,headers={"User-Agent":UA,"Accept":"text/html,application/xml,*/*;q=0.8"},timeout=TIMEOUT,allow_redirects=True)
    r.raise_for_status()
    return r

def cities():
    with CITY_FILE.open("r",encoding="utf-8-sig",newline="") as f: rows=list(csv.DictReader(f))
    lookup={};points=[]
    for r in rows:
        cid=txt(r.get("city_id")); c=txt(r.get("city")).casefold(); s1=txt(r.get("state_code")).casefold(); s2=txt(r.get("state")).casefold(); cc=txt(r.get("country_code")).casefold()
        if cc in ("usa","united states","united states of america"):cc="us"
        for s in {s1,s2}-{""}:
            lookup[(c,s,cc)]=cid;lookup[(c,s,"")]=cid
        try:points.append((cid,float(r["latitude"]),float(r["longitude"])))
        except (ValueError,TypeError,KeyError):pass
    return lookup,points

def miles(a,b,c,d):
    r=3958.7613;p1=math.radians(a);p2=math.radians(c);dp=math.radians(c-a);dl=math.radians(d-b)
    x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return r*2*math.atan2(math.sqrt(max(0,x)),math.sqrt(max(0,1-x)))

def fsq_seeds():
    groups={}
    for citydir in sorted((p for p in FOURSQUARE_ROOT.iterdir() if p.is_dir())):
        files=sorted(citydir.glob("*.json"),key=lambda p:p.name,reverse=True)
        if not files:continue
        try:payload=json.loads(files[0].read_text(encoding="utf-8"))
        except Exception:continue
        city=payload.get("city") or {};cid=txt(city.get("city_id"))
        for search in payload.get("searches") or []:
            for place in (search.get("response") or {}).get("results") or []:
                if not isinstance(place,dict):continue
                web=txt(place.get("website"));o=origin(web)
                if not o:continue
                h=host(o)
                g=groups.setdefault(h,{"host":h,"origin":o,"city_ids":set()})
                if cid:g["city_ids"].add(cid)
    return list(groups.values())

def sitemap_roots(o):
    out=[]
    try:
        r=req(o.rstrip("/")+"/robots.txt")
        for line in r.text.splitlines():
            if ":" in line and line.split(":",1)[0].strip().casefold()=="sitemap":
                u=line.split(":",1)[1].strip()
                if u and u not in out:out.append(u)
    except Exception:pass
    for p in ("/wp-sitemap.xml","/sitemap.xml","/sitemap_index.xml","/sitemap-index.xml"):
        u=o.rstrip("/") + p
        if u not in out:out.append(u)
    return out

def parse_map(data):
    root=ET.fromstring(data);kind=root.tag.rsplit("}",1)[-1].casefold();out=[]
    for e in root.iter():
        if e.tag.rsplit("}",1)[-1].casefold()=="loc" and e.text:
            out.append(("sitemap" if kind=="sitemapindex" else "url",e.text.strip()))
    return out

def sitemap_urls(site):
    q=sitemap_roots(site["origin"]);seen=set();urls=set();files=0;seen_urls=0
    while q and files<SITEMAP_MAX:
        sm=q.pop(0)
        if sm in seen or not same_host(sm,site["host"]):continue
        seen.add(sm)
        try:entries=parse_map(req(sm).content)
        except Exception:continue
        files+=1; named=any(x in sm.casefold() for x in SITEMAP_HINTS)
        for kind,u in entries:
            if kind=="sitemap":
                if u not in seen:q.append(u)
                continue
            if seen_urls>=SITEMAP_URL_MAX:break
            seen_urls+=1
            if not same_host(u,site["host"]):continue
            p=(urlparse(u).path+"?"+urlparse(u).query).casefold()
            score=sum(2 for x in PATH_HINTS if x in p)+(5 if named else 0)
            if re.search(r"/20\d{2}(?:/|-)\d{1,2}(?:/|-)\d{1,2}",p):score+=2
            if score:urls.add((score,u))
    ordered=[u for _,u in sorted(urls,key=lambda x:(-x[0],x[1]))]
    return ordered[:EVENT_URL_MAX],files,seen_urls

def types(v):
    if isinstance(v,str):return [v.rsplit("/",1)[-1]]
    if isinstance(v,list):
        z=[]
        for x in v:z+=types(x)
        return z
    return []

def walk(v):
    if isinstance(v,dict):
        yield v
        for x in v.values():
            if isinstance(x,(dict,list)):yield from walk(x)
    elif isinstance(v,list):
        for x in v:yield from walk(x)

def is_event(v):
    t=types(v.get("@type")) if isinstance(v,dict) else []
    return any(x in EVENT_TYPES or x.endswith("Event") for x in t)

def loc_info(e):
    vals=e.get("location");vals=vals if isinstance(vals,list) else [vals]
    names=[];cities_=[];regions=[];countries=[];streets=[];postals=[];coords=[];lts=[];vus=[]
    for l in vals:
        if not isinstance(l,dict):continue
        lt=types(l.get("@type"));lts+=lt
        n=txt(l.get("name"));u=txt(l.get("url"))
        if n:names.append(n)
        if "VirtualLocation" in lt and u:vus.append(u)
        a=l.get("address")
        if isinstance(a,dict):
            for key,arr in (("streetAddress",streets),("addressLocality",cities_),("addressRegion",regions),("postalCode",postals),("addressCountry",countries)):
                x=txt(a.get(key))
                if x:arr.append(x)
        g=l.get("geo")
        if isinstance(g,dict):
            la=txt(g.get("latitude"));lo=txt(g.get("longitude"))
            if la or lo:coords.append(f"{la},{lo}")
    info={
        "name":"|".join(dict.fromkeys(names)),
        "city":"|".join(dict.fromkeys(cities_)),
        "region":"|".join(dict.fromkeys(regions)),
        "country":"|".join(dict.fromkeys(countries)),
        "street":"|".join(dict.fromkeys(streets)),
        "postal":"|".join(dict.fromkeys(postals)),
        "coords":"|".join(dict.fromkeys(coords)),
        "types":"|".join(dict.fromkeys(lts)),
        "virtual_url":"|".join(dict.fromkeys(vus))
    }
    info["physical"]=bool(streets or cities_ or regions or postals or coords) or any(x in {"Place","MusicVenue","StadiumOrArena","CivicStructure"} for x in lts)
    info["virtual"]=bool(vus) or "VirtualLocation" in lts
    return info

def coord(info):
    if not info["coords"]:return None
    try:a,b=info["coords"].split("|",1)[0].split(",",1);a=float(a);b=float(b)
    except (ValueError,TypeError):return None
    return (a,b) if -90<=a<=90 and -180<=b<=180 else None

def map_city(info,seed_ids,lookup,points):
    c=info["city"].split("|",1)[0].casefold();s=info["region"].split("|",1)[0].casefold();cc=info["country"].split("|",1)[0].casefold()
    if cc in ("usa","united states","united states of america"):cc="us"
    if c and s:
        cid=lookup.get((c,s,cc)) or lookup.get((c,s,""))
        if cid:return cid,"mapped_exact",0
    xy=coord(info)
    if xy:
        near=min(((miles(xy[0],xy[1],p[1],p[2]),p[0]) for p in points),default=None)
        if near and near[0]<=FALLBACK_MILES:return near[1],"mapped_radius",near[0]
    seeds={x for x in seed_ids if x}
    if info["physical"] and len(seeds)==1 and not c and not xy:return next(iter(seeds)),"mapped_seed_site",None
    return None,"unmapped",None

def event_key(e,url):
    u=txt(e.get("url"))
    return ("url",u.casefold()) if u else ("composite",txt(e.get("name")).casefold(),txt(e.get("startDate")).casefold(),url.casefold())

def virtual_only(e,l):
    mode=txt(e.get("eventAttendanceMode")).casefold()
    return not l["physical"] and ("onlineeventattendancemode" in mode or l["virtual"])

def radio(e,l):
    if l["physical"]:return False
    t=" ".join(txt(e.get(x)) for x in ("name","description","keywords")).casefold()
    return any(x in t for x in RADIO_MARKERS)

def json_cell(v):
    return "" if v in (None,"",[],{}) else json.dumps(v,ensure_ascii=False,separators=(",",":"))

def fetch_page(item):
    url,robot=item
    if robot:
        try:
            if not robot.can_fetch(UA,url):return url,None,"robots"
        except Exception:pass
    try:
        r=req(url);ct=r.headers.get("Content-Type","").casefold()
        if ct and "html" not in ct and "xhtml" not in ct:return url,None,"non_html"
        return url,r.text,""
    except Exception as e:return url,None,f"{type(e).__name__}: {e}"

def process(site,lookup,points,run_text):
    urls,sm_files,sm_seen=sitemap_urls(site);robot=None
    try:
        rr=req(site["origin"].rstrip("/")+"/robots.txt");robot=robotparser.RobotFileParser();robot.parse(rr.text.splitlines())
    except Exception:pass
    rows={};seen=set()
    stats={"sitemap_files":sm_files,"sitemap_urls_seen":sm_seen,"candidate_urls":len(urls),"pages_fetched":0,"pages_with_events":0,"event_objects":0,"events_written":0,"virtual_filtered":0,"radio_filtered":0,"unmapped":0,"robots_blocked":0,"page_errors":0}
    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
        futures=[ex.submit(fetch_page,(u,robot)) for u in urls]
        for f in as_completed(futures):
            url,html,err=f.result()
            if err=="robots":stats["robots_blocked"]+=1;continue
            if html is None:stats["page_errors"]+=1;continue
            stats["pages_fetched"]+=1
            try:
                data=extruct.extract(html,base_url=url,syntaxes=["json-ld"],uniform=True)
                events=[];seen_page=set()
                for block in data.get("json-ld",[]):
                    for e in walk(block):
                        if is_event(e):
                            k=event_key(e,url)
                            if k not in seen_page:seen_page.add(k);events.append(e)
            except Exception:
                stats["page_errors"]+=1;continue
            if not events:continue
            stats["pages_with_events"]+=1;stats["event_objects"]+=len(events)
            for e in events:
                l=loc_info(e)
                if virtual_only(e,l):stats["virtual_filtered"]+=1;continue
                if radio(e,l):stats["radio_filtered"]+=1;continue
                if not txt(e.get("name")) or not txt(e.get("startDate")):continue
                k=event_key(e,url)
                if k in seen:continue
                seen.add(k)
                cid,method,d=map_city(l,site["city_ids"],lookup,points)
                if not cid:stats["unmapped"]+=1;continue
                row={
                    "schema_org_run_started_utc":run_text,
                    "schema_org_seed_host":site["host"],
                    "schema_org_seed_city_id":"|".join(sorted(site["city_ids"])),
                    "schema_org_source_url":url,
                    "schema_org_event_type":"|".join(types(e.get("@type"))),
                    "schema_org_city_mapping":method,
                    "schema_org_city_mapping_distance_miles":"" if d is None else f"{d:.3f}",
                    "event_name":txt(e.get("name")),
                    "event_start_date":txt(e.get("startDate")),
                    "event_end_date":txt(e.get("endDate")),
                    "event_url":txt(e.get("url")) or url,
                    "event_status":txt(e.get("eventStatus")),
                    "event_attendance_mode":txt(e.get("eventAttendanceMode")),
                    "event_description":txt(e.get("description")),
                    "event_location_json":json_cell(e.get("location")),
                    "event_offers_json":json_cell(e.get("offers")),
                    "event_organizer_json":json_cell(e.get("organizer")),
                    "event_performer_json":json_cell(e.get("performer")),
                    "event_image_json":json_cell(e.get("image")),
                    "event_keywords":txt(e.get("keywords")),
                    "event_json":json_cell(e)
                }
                rows.setdefault(cid,[]).append(row);stats["events_written"]+=1
    return rows,stats

def write_files(run_date,rows_by_city):
    fields=[
        "schema_org_run_started_utc","schema_org_seed_host",
        "schema_org_seed_city_id","schema_org_source_url",
        "schema_org_event_type","schema_org_city_mapping",
        "schema_org_city_mapping_distance_miles","event_name",
        "event_start_date","event_end_date","event_url",
        "event_status","event_attendance_mode","event_description",
        "event_location_json","event_offers_json",
        "event_organizer_json","event_performer_json",
        "event_image_json","event_keywords","event_json"
    ]
    files=0;rows=0
    for cid,rs in sorted(rows_by_city.items()):
        rs.sort(key=lambda x:(x["event_start_date"],x["event_name"],x["event_url"]))
        for off in range(0,len(rs),1000):
            chunk=rs[off:off+1000]
            p=RAW_ROOT/cid/run_date/f"page_{off//1000:03d}.csv"
            p.parent.mkdir(parents=True,exist_ok=True)
            with p.open("w",encoding="utf-8-sig",newline="") as f:
                w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(chunk)
            files+=1;rows+=len(chunk)
    return files,rows

def clear_run(run_date):
    if not RAW_ROOT.exists():return
    for d in RAW_ROOT.iterdir():
        if not d.is_dir():continue
        dd=d/run_date
        if dd.exists():
            for p in dd.glob("page_*.csv"):p.unlink()
            try:dd.rmdir()
            except OSError:pass

def main():
    started=datetime.now(timezone.utc)
    run_date=started.strftime("%Y%m%d")
    run_text=started.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        lookup,points=cities()
        sites=fsq_seeds()
    except Exception as e:
        print("SCHEMA.ORG: FAILED");print(f"ERROR: {e}");return 2
    if not sites:
        print("SCHEMA.ORG: FAILED");print("ERROR: no Foursquare website seeds");return 2
    clear_run(run_date)
    print(f"WEBSITE SEEDS: {len(sites)}",flush=True)
    totals={k:0 for k in ("sitemap_files","sitemap_urls_seen","candidate_urls","pages_fetched","pages_with_events","event_objects","events_written","virtual_filtered","radio_filtered","unmapped","robots_blocked","page_errors")}
    all_rows={}
    with ThreadPoolExecutor(max_workers=SITEMAP_WORKERS) as ex:
        futures={ex.submit(process,s,lookup,points,run_text):s["host"] for s in sites}
        done=0
        for f in as_completed(futures):
            done+=1;h=futures[f]
            try:rows,st=f.result()
            except Exception as e:
                print(f"SITE ERROR {h}: {type(e).__name__}: {e}",flush=True);continue
            for k in totals:totals[k]+=st[k]
            for cid,rs in rows.items():all_rows.setdefault(cid,[]).extend(rs)
            print(f"SITES {done}/{len(sites)} {h}: candidates={st['candidate_urls']} events={st['event_objects']} written={st['events_written']}",flush=True)
    files,rows=write_files(run_date,all_rows)
    print("")
    print("SCHEMA.ORG: PASS")
    print(f"RUN DATE: {run_date}")
    print(f"WEBSITE SEEDS: {len(sites)}")
    print(f"SITEMAP FILES: {totals['sitemap_files']}")
    print(f"SITEMAP URLS SEEN: {totals['sitemap_urls_seen']}")
    print(f"EVENT URL CANDIDATES: {totals['candidate_urls']}")
    print(f"PAGES FETCHED: {totals['pages_fetched']}")
    print(f"PAGES WITH EVENTS: {totals['pages_with_events']}")
    print(f"EVENT OBJECTS: {totals['event_objects']}")
    print(f"VIRTUAL-ONLY FILTERED: {totals['virtual_filtered']}")
    print(f"RADIO FILTERED: {totals['radio_filtered']}")
    print(f"UNMAPPED EVENTS: {totals['unmapped']}")
    print(f"ROBOTS BLOCKED: {totals['robots_blocked']}")
    print(f"PAGE ERRORS: {totals['page_errors']}")
    print(f"EVENTS WRITTEN: {rows}")
    print(f"CITIES WRITTEN: {len(all_rows)}")
    print(f"CSV FILES: {files}")
    print(f"OUTPUT: data/events/raw/schema_org/<city_id>/{run_date}/page_###.csv")
    return 0

if __name__=="__main__":
    raise SystemExit(main())