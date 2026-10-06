const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
function unit(name) {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`));
  assert.ok(match, `${name} exists`);
  return match[0];
}
class Element {
  constructor() { this.children = []; this.textContent = ''; this.hidden = false; }
  appendChild(child) { this.children.push(child); child.parent = this; }
  replaceChildren() { this.children = []; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
}
const projection = (count = 4) => ({ record_status: count === 4 ? 'complete' : 'incomplete', data: {
  career_explorer: {status: count === 4 ? 'complete' : 'incomplete', completed_count: count, holland_code: 'IRC'},
}});
function fixture() {
  const ids = Object.fromEntries(['prep-chat-messages', 'prep-chat-engine-label', 'roadmap-status'].map(id => [id, new Element()]));
  const pending = [], displayed = [], guidance = [];
  const context = {
    state: { student: {email: 'synthetic-a@ensign.edu'}, assessmentData: {}, guidance: null },
    readinessLookupVersion: 1, prepChatVersion: 0,
    $: selector => ids[selector.slice(1)] || null,
    document: {createElement: () => new Element()},
    assessmentDataFromReadiness: ce => ce || {status: 'not_found', completed_count: 0},
    connectStatusFromReadiness: () => ({}), renderAssessmentCards() {}, renderReadinessFreshness() {}, renderVmockReadiness() {},
    readinessGaps: () => [], showLookupFeedback() {}, renderGuidanceDisplay: data => guidance.push(data),
    appendPrepChatMessage: (role, text) => displayed.push({role, text}), console,
    fetch: (url, options) => new Promise(resolve => pending.push({url, body: JSON.parse(options.body), resolve})),
  };
  vm.createContext(context);
  vm.runInContext('let prepChatHistory = [];', context);
  if (source.includes('function resetPrepChat(')) vm.runInContext(unit('resetPrepChat'), context);
  else context.resetPrepChat = () => {};
  for (const name of ['fetchCareerGuidance','applyReadinessProjection','sendPrepChatMessage']) vm.runInContext(unit(name), context);
  const reply = (request, body, ok = true) => request.resolve({ok, json: async () => body});
  return {context, pending, displayed, guidance, ids, reply};
}
async function main() {
  // Existing conversations must not prevent auto-generation or delay it behind recommendations.
  {
    const f = fixture();
    vm.runInContext("prepChatHistory = [{role:'assistant',content:'Previous appointment'}]", f.context);
    const work = f.context.applyReadinessProjection(projection());
    assert.equal(f.pending.filter(x => x.url === '/api/chat').length, 1, 'Synthesis starts immediately even after prior chat');
    assert.equal(f.pending.filter(x => x.url.endsWith('/guidance')).length, 1);
    const chat = f.pending.find(x => x.url === '/api/chat');
    assert.equal(chat.body.history.length, 0, 'Previous student conversation is excluded');
    assert.match(chat.body.message, /Executive Summary/);
    assert.match(chat.body.message, /Career.*Major/);
    f.reply(chat, {reply: 'Executive Summary A', engine: 'local'});
    f.reply(f.pending.find(x => x.url.endsWith('/guidance')), {status: 'ok', aligned_careers: ['Synthetic Career']});
    await work;
    assert.equal(f.displayed.filter(x => x.role === 'assistant').length, 1);
    // A new/repeated lookup must regenerate after the previous result or a failed attempt.
    const retry = f.context.applyReadinessProjection(projection());
    const newRequests = f.pending.slice(2);
    assert.equal(newRequests.length, 2);
    assert.equal(newRequests.find(x => x.url === '/api/chat').body.history.length, 0);
    newRequests.forEach(x => f.reply(x, x.url === '/api/chat' ? {reply: 'Executive Summary retry'} : {status:'ok'}));
    await retry;
  }
  // Partial assessment results still receive a briefing, with missing data left to the existing engine.
  {
    const f = fixture();
    const work = f.context.applyReadinessProjection(projection(2));
    assert.equal(f.pending.length, 2, 'Available partial results generate summary and recommendations');
    f.pending.forEach(x => f.reply(x, x.url === '/api/chat' ? {reply:'Partial results'} : {status:'ok'}));
    await work;
  }
  // No assessment means no invented AI synthesis or recommendations.
  {
    const f = fixture();
    await f.context.applyReadinessProjection({record_status:'not_found', data:{}});
    assert.equal(f.pending.length, 0);
    await f.context.applyReadinessProjection(projection(0));
    assert.equal(f.pending.length, 0);
  }
  // A late success or error from a previous student must not mutate the next student's UI/history.
  for (const oldFails of [false, true]) {
    const f = fixture();
    const old = f.context.applyReadinessProjection(projection());
    const requests = [...f.pending];
    f.context.readinessLookupVersion++;
    f.context.state = {student:{email:'synthetic-b@ensign.edu'},assessmentData:{},guidance:null};
    f.context.resetPrepChat();
    const current = f.context.applyReadinessProjection(projection());
    const newRequests = f.pending.slice(2);
    newRequests.forEach(x => f.reply(x, x.url === '/api/chat' ? {reply:'Executive Summary B',engine:'local'} : {status:'ok',owner:'B'}));
    await current;
    const before = f.displayed.length;
    requests.forEach(x => f.reply(x, x.url === '/api/chat' ? (oldFails ? {error:'Old failure'} : {reply:'Old student A',engine:'old'}) : {status:'ok',owner:'A'}, !oldFails));
    await old;
    assert.equal(f.displayed.length, before, 'Old response ignored');
    assert.equal(f.guidance.length, 1, 'Old recommendations ignored');
    assert.equal(f.context.state.guidance.owner, 'B');
    assert.ok(!vm.runInContext('JSON.stringify(prepChatHistory)', f.context).includes('Old student'));
  }
  // Actual HTTP errors must be visible and retries must remain possible.
  {
    const f = fixture();
    const work = f.context.sendPrepChatMessage('Executive Summary');
    f.reply(f.pending[0], {error:'Please retry'}, false);
    await work;
    assert.equal(f.displayed.at(-1).text, 'Please retry');
  }
  console.log('Automatic assessment: prior chat, immediate parallel start, retry, partial/missing, student race, HTTP error PASS');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
