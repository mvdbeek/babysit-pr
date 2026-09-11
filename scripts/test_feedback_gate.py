import argparse
import copy
import json
import http.client
import time

import pytest

import dashboard
import gh_pr_watch as watch
import pr_supervisor as supervisor
from test_pr_supervisor import harness, job, snapshot, release_fixture, start_service, wait_until
from test_dashboard_cancel import server, saved


def feedback_job():
    j = job()
    s = snapshot()
    s.update(failed_jobs=[], failed_runs=[], new_review_items=[])
    s['checks'].update(failed_count=0, passed_count=2)
    j.update(snapshot=s, attempts=0, max_repairs=5, epoch=0, branch=None,
             pending_reviews=[{'kind':'issue_comment','id':'1','author':'reviewer',
                               'body':'UNAPPROVED_PAYLOAD','url':'https://github.com/test/repo/issues/1#issuecomment-1'}])
    return j


def persist(db, j):
    with db:
        supervisor.save_job(db, j)


def approve(db, j):
    persist(db, j)
    return supervisor.approve_feedback(db, j['id'], supervisor.feedback_token(j['pending_reviews']))


def test_feedback_cannot_wake_without_click_or_leak_into_ci_prompt():
    j = feedback_job()
    j['snapshot']['new_review_items'] = copy.deepcopy(j['pending_reviews'])
    assert not supervisor.actionable(j)
    j['instructions'] = 'Fix CI'
    assert 'UNAPPROVED_PAYLOAD' not in supervisor.repair_prompt(j)
    j['snapshot']['checks']['failed_count'] = 1
    assert supervisor.actionable(j)
    assert 'UNAPPROVED_PAYLOAD' not in supervisor.repair_prompt(j)


def test_approval_binds_content_and_leaves_new_items_pending(tmp_path, monkeypatch):
    db = supervisor.open_db(tmp_path)
    j = approve(db, feedback_job())
    assert supervisor.actionable(j)
    j['pending_reviews'].append({'kind':'issue_comment','id':'2','body':'LATER_PAYLOAD'})
    j.update(cwd=str(tmp_path), session_id='fixture', instructions='Handle approved feedback', repair_timeout=30)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *args, **kwargs: None)
    supervisor.start_repair(db, tmp_path, j)
    assert j['approved_reviews'] == []
    prompt = supervisor.repair_prompt(j)
    assert 'UNAPPROVED_PAYLOAD' in prompt and 'LATER_PAYLOAD' not in prompt
    supervisor.finish_repair(j, {'status':'waiting','summary':'Done'})
    assert [i['id'] for i in j['pending_reviews']] == ['2']
    assert not supervisor.actionable(j)
    db.close()


def test_stale_click_and_edited_approved_body_require_new_approval(tmp_path):
    db = supervisor.open_db(tmp_path)
    j = feedback_job()
    old_token = supervisor.feedback_token(j['pending_reviews'])
    j['pending_reviews'][0]['body'] = 'Edited'
    persist(db, j)
    with pytest.raises(ValueError, match='changed'):
        supervisor.approve_feedback(db, j['id'], old_token)
    j = approve(db, j)
    j['pending_reviews'][0]['body'] = 'Edited again'
    assert not supervisor.actionable(j)
    db.close()


@pytest.mark.parametrize('state', ['paused','running','blocked','closed','stopped','awaiting_release'])
def test_approval_never_releases_or_revives_watch(tmp_path, state):
    db = supervisor.open_db(tmp_path)
    j = feedback_job()
    j['status'] = state
    persist(db, j)
    with pytest.raises(ValueError):
        supervisor.approve_feedback(db, j['id'], supervisor.feedback_token(j['pending_reviews']))
    assert supervisor.get_job(db, j['id'])['status'] == state
    db.close()


def test_failed_attempt_consumes_approval_but_keeps_feedback(tmp_path, monkeypatch):
    db = supervisor.open_db(tmp_path)
    j = approve(db, feedback_job())
    j.update(cwd=str(tmp_path), session_id='fixture', instructions='Handle approved feedback', repair_timeout=30)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', lambda *args, **kwargs: None)
    supervisor.start_repair(db, tmp_path, j)
    supervisor.finish_repair(j, {'status':'blocked','summary':'Needs help'})
    assert j['pending_reviews'] and not supervisor.approved_feedback(j)
    db.close()


@pytest.mark.parametrize('merged', [False, True])
def test_closed_snapshot_needs_no_comments_or_ci_and_never_wakes(tmp_path, monkeypatch, merged):
    pr = {**snapshot()['pr'], 'repo':'test/repo','number':1, 'closed':not merged,'merged':merged}
    monkeypatch.setattr(watch, 'resolve_subject', lambda *a, **kw: pr)
    def unexpected(*args, **kwargs):
        raise AssertionError('No comment/CI APIs should be accessed after closure')
    for name in ['fetch_new_review_items','get_authenticated_login','get_pr_checks','get_workflow_runs_for_sha','resolve_ci_repo']:
        monkeypatch.setattr(watch, name, unexpected)
    args = argparse.Namespace(pr='1', repo='test/repo', state_file=str(tmp_path/'state'), ci_repo='head')
    state = {}
    result, _ = watch.collect_snapshot(args, state, persist=False)
    j = feedback_job()
    j['approved_reviews'] = [supervisor.feedback_token(j['pending_reviews'][0])]
    supervisor.ingest(j, result, state)
    assert j['status'] == 'closed' and j['cleanup_ready']
    assert not supervisor.actionable(j)
    view = dashboard.present_job(j)
    assert view['cleanup_ready'] and view['pr_outcome'] == ('merged' if merged else 'closed')


def test_feedback_http_requires_matching_batch_and_same_origin(server):
    db = supervisor.open_db(server.home)
    j = feedback_job()
    persist(db, j)
    db.close()
    token = dashboard.present_job(j)['feedback_token']
    def post(origin, token):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
        conn.request('POST','/api/feedback',json.dumps({'id':j['id'],'token':token}),
                     {'Content-Type':'application/json','X-Babysit-Action':'feedback',
                      'Host':'example.ts.net:8443','Origin':origin})
        r = conn.getresponse()
        code, data = r.status, json.loads(r.read())
        conn.close()
        return code, data
    assert post('https://evil.invalid', token)[0] == 403
    assert post('https://example.ts.net:8443', 'stale')[0] == 400
    assert not saved(server,j['id']).get('approved_reviews')
    code, response = post('https://example.ts.net:8443',token)
    assert code == 200 and response['job']['feedback_approved'] == 1


def test_edited_comments_are_resurfaced_for_approval(monkeypatch):
    payload = [{'id':1,'body':'original','user':{'login':'trusted'},'author_association':'MEMBER'}]
    monkeypatch.setattr(watch, 'gh_api_list_paginated', lambda endpoint, **kw: payload if '/issues/' in endpoint else [])
    pr = {'repo':'test/repo','number':1}
    state = {}
    assert len(watch.fetch_new_review_items(pr,state,True)) == 1
    assert watch.fetch_new_review_items(pr,state,False) == []
    payload[0]['body'] = 'edited'
    assert watch.fetch_new_review_items(pr,state,False)[0]['body'] == 'edited'
    assert watch.fetch_new_review_items(pr,state,False) == []


def test_real_guardian_receives_only_approved_feedback(harness):
    h = harness
    state = json.loads(h['state'].read_text())
    state['failed'] = False
    h['state'].write_text(json.dumps(state))
    release_fixture(h)
    j = supervisor.get_job(h['db'],h['job']['id'])
    j['pending_reviews'] = feedback_job()['pending_reviews']
    persist(h['db'], j)
    proc = start_service(h)
    try:
        wait_until(lambda: supervisor.get_job(h['db'],j['id']).get('snapshot'))
        assert not h['calls'].exists()
        current = supervisor.get_job(h['db'],j['id'])
        supervisor.approve_feedback(h['db'],j['id'],supervisor.feedback_token(current['pending_reviews']))
        wait_until(lambda: supervisor.get_job(h['db'],j['id'])['attempts']==1 and supervisor.get_job(h['db'],j['id'])['status']=='watching')
        calls = [json.loads(line) for line in h['calls'].read_text().splitlines()]
        assert len(calls)==1 and 'UNAPPROVED_PAYLOAD' in calls[0]['prompt']
        proc.terminate()
        proc.wait(timeout=5)
        proc = start_service(h)
        time.sleep(2)
        assert len(h['calls'].read_text().splitlines())==1
    finally:
        proc.terminate()
        proc.wait(timeout=5)
