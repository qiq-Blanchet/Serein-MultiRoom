import { initializeRoomScope } from '../storage/roomScopedStorage.js';
import { readGardenSnapshot } from './data.js';
import { memoryLayout, projectMemories } from './memories.js';
import './style.css';

const loader = document.querySelector('#memory-loading');
const message = document.querySelector('#memory-loading-message');
const retry = document.querySelector('#memory-retry');
const controls = [...document.querySelectorAll('.tools button')];
const embedded = window.parent !== window;

function reveal() {
  if (embedded) window.parent.postMessage({ type: 'serein:garden-ready' }, location.origin);
}

async function openGarden() {
  loader.hidden = false;
  retry.hidden = true;
  controls.forEach(button => { button.disabled = true; });
  message.textContent = '记忆正在回到花园…';
  try {
    const [{ startGarden }, payload, { createMemoryDetails }] = await Promise.all([
      import('./main.js'), readGardenSnapshot(), import('./details.jsx'),
    ]);
    const { records, relations } = projectMemories(payload);
    if (!records.length) {
      message.textContent = '还没有可展示的记忆。';
      retry.hidden = false;
      reveal();
      return;
    }
    const details = createMemoryDetails();
    startGarden({ records, relations, layout: memoryLayout(records), details });
    const scenes = records.filter(record => record.kind === 'scene').length;
    document.querySelector('#data-note').textContent = `${scenes} Scene · ${records.length - scenes} Event`;
    window.__gardenStudy.source = { name: payload.source, readAt: payload.readAt, snapshotId: payload.snapshotId };
    loader.hidden = true;
    controls.forEach(button => { button.disabled = false; });
    // Let the first rendered frame reach the screen before lifting the black transition.
    requestAnimationFrame(() => requestAnimationFrame(reveal));
  } catch {
    message.textContent = '花园暂时没能展开，请稍后重试。';
    retry.hidden = false;
    reveal();
  }
}

const back = document.querySelector('#garden-back');
back.hidden = embedded;
back.addEventListener('click', () => { location.href = './#memory'; });
retry.addEventListener('click', openGarden);
try {
  await initializeRoomScope();
  openGarden();
} catch {
  message.textContent = '无法验证当前 AI 房间。请重新打开已授权的记忆库。';
  retry.hidden = true;
  reveal();
}
