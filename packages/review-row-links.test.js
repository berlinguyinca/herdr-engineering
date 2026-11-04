import test from 'node:test';
import assert from 'node:assert/strict';
import {integrationRow} from './integrations-view.js';

const entry = record => ({id: 'r1', tab: 'finished', record});

test('a review row links to the agent, its Evolution tab and the PR trail it names', () => {
  const html = integrationRow(entry({review_id: 'r1', kind: 'code', status: 'approved', repository: 'org/app', pull_request_number: 7, session_id: 's 1'}), 'reviews');
  assert.match(html, /href="#\/herd\/session\/s%201"/);
  assert.match(html, /href="#\/herd\/session\/s%201\?tab=evolution"/);
  assert.match(html, /href="#\/pr\/org\/app\/7"[^>]*>PR #7 trail</);
});

test('a forge host on the repository is carried to the trail link', () => {
  const html = integrationRow(entry({review_id: 'r1', kind: 'code', repository: 'ghe.example.com/org/app', pull_request_number: 3}), 'reviews');
  assert.match(html, /href="#\/pr\/org\/app\/3\?host=ghe\.example\.com"/);
});

test('no PR link without a PR number or with a bad one; nothing is guessed from the branch', () => {
  for (const record of [{repository: 'org/app', branch: 'pr-7'}, {repository: 'org/app', pull_request_number: 0}, {repository: 'org/app', pull_request_number: '7'}, {pull_request_number: 7}]) {
    assert.doesNotMatch(integrationRow(entry({review_id: 'r1', kind: 'plan', ...record}), 'reviews'), /#\/pr\//);
  }
});

test('CI rows are unchanged', () => {
  assert.doesNotMatch(integrationRow(entry({run_number: 5, repository: 'org/app', pull_request_number: 7}), 'ci'), /trail|tab=evolution/);
});
