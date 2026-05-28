const constraintList     = document.getElementById('constraintList');
const chatForm           = document.getElementById('chatForm');
const chatInput          = document.getElementById('chatInput');
const chatWindow         = document.getElementById('chatWindow');
const createScheduleBtn  = document.getElementById('createScheduleBtn');



const welcome_msg =
  "Hi, I am a knowledge engineer here to help you build your schedule! " +
  "Please add information about your worker's availabilities, or some constraints the schedule needs to satisfy.";


// we always keep whole chat history in memory with role tags to keep track of who said what, starts with just welcome msg
const chatHistory = [{ role: 'assistant', content: welcome_msg }];

// on page load, we display the welcome message, load the knowledge base and the current availability info (empty at first)
window.addEventListener('DOMContentLoaded', async () => {
  appendMessage(welcome_msg, 'response');
  await loadKnowledgeBase();
  await loadAvailability();
});

// load knowledge base constraints from server
async function loadKnowledgeBase() {
  try {
    const res = await fetch('/api/constraints');
    const items = await res.json();
    for (const it of items) {
      addConstraint(it.nl, it.id);
    }
  } catch (err) {
    console.error('Could not load knowledge base:', err);
  }
}

// load availability info from server
async function loadAvailability() {
  try {
    const res = await fetch('/api/availability');
    const workers = await res.json();
    const existingRows = constraintList.querySelectorAll('li[data-kind="availability"]');
    for (const row of existingRows) row.remove();
    for (const worker of workers) {
      addAvailabilityRow(worker.name, worker.summary);
    }
  } catch (err) {
    console.error('Could not load availability:', err);
  }
}

// post request when the user submits new msg 
chatForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;
  chatInput.value = '';

  appendMessage(text, 'prompt');

  try {
    const res = await fetch('/api/translate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, history: chatHistory }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `System error ${res.status}`);
    }

    // process response: display assistant's reply, add new constraint (if any), update availability info (if needed)
    const data = await res.json();
    const reply = data.result;

    chatHistory.push({ role: 'user',      content: text });
    chatHistory.push({ role: 'assistant', content: reply });

    appendMessage(reply, 'response');

    if (data.is_valid) {
      addConstraint(data.nl || text, data.id);
    }

    if (Array.isArray(data.removed_ids) && data.removed_ids.length) {
      for (const id of data.removed_ids) {
        removeConstraintFromDOM(id);
      }
    }

    const touchedWorkers = (
      (Array.isArray(data.added_workers)        && data.added_workers.length)        ||
      (Array.isArray(data.removed_workers)      && data.removed_workers.length)      ||
      (Array.isArray(data.availability_changes) && data.availability_changes.length)
    );
    if (touchedWorkers) {
      await loadAvailability();
    }
  } catch (err) {
    appendMessage(`Error: ${err.message}`, 'response');
  }
});

/* ---------- create schedule section ---------- */


// post request to create schedule when user clicks the button 
createScheduleBtn.addEventListener('click', async () => {
  createScheduleBtn.disabled = true;
  appendMessage('Running IDP-Z3 on the current knowledge base…', 'prompt');

  try {
    const res = await fetch('/api/create-schedule', { method: 'POST' });
    const data = await res.json();

    // process respoonse + provide feedback if error, unsat or schedule generated successfully
    if (!data.ok) {
      clearTimeline('Schedule could not be generated.');
      appendMessage(`Could not build schedule: ${data.error}`, 'response');
    } else if (data.unsat) {
      clearTimeline('Schedule could not be generated.');
      appendMessage(data.schedule, 'response', 'mono');
    } else if (data.data) {
      renderTimeline(data.data);
      appendMessage('Schedule generated. See the Schedule panel below.', 'response');
    } else {
      clearTimeline('Schedule could not be parsed.');
      appendMessage(data.schedule, 'response', 'mono');
    }
  } catch (err) {
    appendMessage(`Error: ${err.message}`, 'response');
  } finally {
    createScheduleBtn.disabled = false;
  }
});

// append a new message to the chat window, using bubbles with different styles for user prompts vs assistant responses
function appendMessage(text, role, extraClass = '') {
  const wrap = document.createElement('div');
  wrap.className = `chat-message chat-${role}`;

  const bubble = document.createElement('span');
  bubble.className = `bubble${extraClass ? ' ' + extraClass : ''}`;
  bubble.textContent = text;

  wrap.appendChild(bubble);
  chatWindow.appendChild(wrap);
  chatWindow.scrollTop = chatWindow.scrollHeight;
}


function addConstraint(text, id) {
  const li = document.createElement('li');
  li.className = 'constraint-item';
  li.dataset.kind = 'constraint';
  if (id != null) li.dataset.id = String(id);
  li.textContent = text;
  constraintList.appendChild(li);
}

function removeConstraintFromDOM(id) {
  const li = constraintList.querySelector(`li[data-kind="constraint"][data-id="${id}"]`);
  if (li) li.remove();
}

function addAvailabilityRow(workerName, summary) {
  const li = document.createElement('li');
  li.className = 'constraint-item';
  li.dataset.kind = 'availability';
  li.textContent = summary;
  constraintList.appendChild(li);
}

/* ---------- Schedule timeline ---------- */

// fetches the current schedule on page load and draws it
async function loadSchedule() {
  console.log('[schedule] loading…');
  const container = document.getElementById('scheduleTimeline');
  if (!window.vis) {
    console.error('[schedule] vis-timeline library failed to load from CDN');
    if (container) container.textContent = 'vis-timeline failed to load.';
    return;
  }
  try {
    const res = await fetch('/api/schedule');
    if (!res.ok) throw new Error(`Failed to load schedule (${res.status})`);
    const data = await res.json();
    console.log('[schedule] data', data);
    renderTimeline(data);
    console.log('[schedule] rendered');
  } catch (err) {
    console.error('[schedule] load failed:', err);
    if (container) container.textContent = `Could not load schedule: ${err.message}`;
  }
}

let currentTimeline = null;

function clearTimeline(message = null) {
  const container = document.getElementById('scheduleTimeline');
  if (!container) return;
  if (currentTimeline) {
    try { currentTimeline.destroy(); } catch (e) {}
    currentTimeline = null;
  }
  container.innerHTML = '';
  if (message) {
    const div = document.createElement('div');
    div.className = 'schedule-error';
    div.textContent = message;
    container.appendChild(div);
  }
}

function renderTimeline(data) {
  const container = document.getElementById('scheduleTimeline');
  if (!container || !window.vis) return;

  if (currentTimeline) {
    try { currentTimeline.destroy(); } catch (e) {}
    currentTimeline = null;
  }
  container.innerHTML = '';

  const groups = new vis.DataSet(
    Object.entries(data.shifts).map(([id, s]) => ({ id, content: s.label }))
  );

  const items = new vis.DataSet();
  let itemId = 0;
  for (const a of data.assignments) {
    const shift = data.shifts[a.shift];
    const tags = a.nurses
      .map(n => `<span class="nurse-tag">${escapeHTML(n)}</span>`)
      .join('');
    items.add({
      id: itemId++,
      group: a.shift,
      content: `<div class="nurse-cell">${tags}</div>`,
      start: `${a.date}T${shift.start}:00`,
      end:   `${a.date}T${shift.end}:00`,
    });
  }

  currentTimeline = new vis.Timeline(container, items, groups, {
    start: data.week_start,
    end:   data.week_end,
    min:   data.week_start,
    max:   data.week_end,
    zoomable:       false,
    moveable:       true,
    showCurrentTime: false,
    orientation:    'top',
    stack:          false,
    margin:         { item: 0, axis: 8 },
    timeAxis:       { scale: 'day', step: 1 },
    locale: 'en',
    format: {
      minorLabels: { day: 'dddd' },
      majorLabels: { day: '' },
    },
    xss: { disabled: true },
  });
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]
  ));
}

loadSchedule();
