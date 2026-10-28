"""Keep complete GHCR builds and bounded Actions/Release history; dry-run by default."""
import argparse
import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

SHA_TAG = re.compile(r'^sha-([0-9a-f]{40})(?:-(?:amd64|arm64))?$')
ROOT_TAG = re.compile(r'^sha-([0-9a-f]{40})$')
SEMVER = re.compile(r'^v?\d+\.\d+\.\d+(?:[-+][\w.-]+)?$')


def tags(version):
    return version.get('metadata', {}).get('container', {}).get('tags', [])


def retained_roots(versions, keep):
    roots = [v for v in versions if any(ROOT_TAG.fullmatch(t) for t in tags(v))]
    roots.sort(key=lambda v:v['created_at'], reverse=True)
    kept = roots[:keep]
    for version in versions:
        if 'latest' in tags(version) and version not in kept:
            kept.append(version)
    return kept


def image_candidates(versions, roots, protected, now):
    keep_shas={m.group(1) for v in roots for tag in tags(v) if (m:=SHA_TAG.fullmatch(tag))}
    all_roots={m.group(1) for v in versions for tag in tags(v) if (m:=ROOT_TAG.fullmatch(tag))}
    out=[]
    for version in versions:
        names=tags(version)
        if version['name'] in protected or 'latest' in names:
            continue
        shas={m.group(1) for name in names if (m:=SHA_TAG.fullmatch(name))}
        if shas & keep_shas:
            continue
        if any(not SHA_TAG.fullmatch(name) and not SEMVER.fullmatch(name) for name in names):
            continue
        age=now-dt.datetime.fromisoformat(version['created_at'].replace('Z','+00:00'))
        # Incomplete concurrent builds get 24 hours to finish; unknown tags are untouched.
        if shas and (shas <= all_roots or age>dt.timedelta(hours=24)):
            out.append(version)
        elif not names and age>dt.timedelta(hours=24):
            out.append(version)
    return sorted(out,key=lambda v:(not any(ROOT_TAG.fullmatch(t) for t in tags(v)),v['created_at']))


def old_runs(runs, keep, current=''):
    ordered=sorted(runs,key=lambda r:r['created_at'],reverse=True)
    preserved={r['id'] for r in ordered[:keep]}
    return [r for r in ordered if r['id'] not in preserved and str(r['id'])!=str(current) and r['status']=='completed']


class GitHub:
    def __init__(self,token):self.token=token
    def call(self,path,method='GET'):
        request=urllib.request.Request('https://api.github.com'+path,method=method,headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'build-retention'})
        try:
            with urllib.request.urlopen(request,timeout=30) as response:
                data=response.read()
            return json.loads(data) if data else None
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'GitHub HTTP {error.code}: {path}') from None
    def pages(self,path,key=None):
        result=[]
        for page in range(1,101):
            data=self.call(path+('&' if '?' in path else '?')+f'per_page=100&page={page}')
            rows=data[key] if key else data
            if not isinstance(rows,list):raise RuntimeError('Unexpected GitHub page')
            result.extend(rows)
            if len(rows)<100:return result
        raise RuntimeError('Pagination limit reached; no deletion attempted')


class Registry:
    def __init__(self,owner,package):
        self.name=owner.lower()+'/'+package
        query=urllib.parse.urlencode({'service':'ghcr.io','scope':'repository:'+self.name+':pull'})
        with urllib.request.urlopen('https://ghcr.io/token?'+query,timeout=30) as response:
            self.token=json.load(response)['token']
    def graph(self,digest,seen=None):
        if seen is None:seen=set()
        if digest in seen:return seen
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or len(seen)>200:raise RuntimeError('Invalid manifest graph; refusing cleanup')
        seen.add(digest)
        request=urllib.request.Request('https://ghcr.io/v2/'+self.name+'/manifests/'+digest,headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'})
        with urllib.request.urlopen(request,timeout=30) as response:data=json.load(response)
        if data.get('schemaVersion')!=2:raise RuntimeError('Unknown image schema; refusing cleanup')
        for child in data.get('manifests',[]):self.graph(child['digest'],seen)
        return seen


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',default=os.environ.get('GITHUB_REPOSITORY',''))
    parser.add_argument('--package',default='')
    parser.add_argument('--workflow',default='docker.yml')
    parser.add_argument('--keep-builds',type=int,default=5)
    parser.add_argument('--keep-runs',type=int,default=3)
    parser.add_argument('--releases',action='store_true')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if '/' not in args.repo or min(args.keep_builds,args.keep_runs)<1:parser.error('Repository and positive retention counts are required')
    token=os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not token:parser.error('GITHUB_TOKEN is required')
    github=GitHub(token);owner=args.repo.split('/')[0]
    def remove(path,label):
        print(('Delete ' if args.apply else 'Would delete ')+label,flush=True)
        if args.apply:github.call(path,'DELETE')
    if args.package:
        kind='orgs' if github.call('/users/'+owner)['type']=='Organization' else 'users'
        base='/'+kind+'/'+owner+'/packages/container/'+urllib.parse.quote(args.package,safe='')
        versions=github.pages(base+'/versions');roots=retained_roots(versions,args.keep_builds)
        if not roots:raise RuntimeError('No complete tagged build found; refusing image cleanup')
        registry=Registry(owner,args.package);protected=set()
        for root in roots:protected.update(registry.graph(root['name']))
        candidates=image_candidates(versions,roots,protected,dt.datetime.now(dt.timezone.utc))
        print(f'Keep {len(roots)} complete image builds and {len(protected)} referenced manifests; remove {len(candidates)} old versions',flush=True)
        for version in candidates:remove(base+'/versions/'+str(version['id']),'image version '+str(version['id']))
    if args.releases:
        releases=[r for r in github.pages('/repos/'+args.repo+'/releases') if not r.get('draft')]
        releases.sort(key=lambda r:r.get('published_at') or r['created_at'],reverse=True)
        for release in releases[args.keep_builds:]:remove('/repos/'+args.repo+'/releases/'+str(release['id']),'release '+release['tag_name'])
    runs=github.pages('/repos/'+args.repo+'/actions/workflows/'+urllib.parse.quote(args.workflow,safe='')+'/runs','workflow_runs')
    for run in old_runs(runs,args.keep_runs,os.environ.get('GITHUB_RUN_ID','')):
        remove('/repos/'+args.repo+'/actions/runs/'+str(run['id']),'workflow run '+str(run['id']))


if __name__=='__main__':main()
