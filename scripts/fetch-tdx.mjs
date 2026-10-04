import { mkdir, writeFile, readFile } from 'node:fs/promises';

const BASE = 'https://tdx.transportdata.tw/api/tourism/service/odata/V2/Tourism';
const { TDX_CLIENT_ID, TDX_CLIENT_SECRET } = process.env;

if (!TDX_CLIENT_ID || !TDX_CLIENT_SECRET) {
  console.error('Missing TDX_CLIENT_ID / TDX_CLIENT_SECRET');
  process.exit(1);
}

// category 必須和 index.html 的 TDX_SOURCES.name 完全一致
const SOURCES = [
  { path: 'Attraction', idField: 'AttractionID', nameField: 'AttractionName',
    category: 'Tourist attractions', icon: 'S', color: '#c96b1a', max: 600 },
  { path: 'Restaurant', idField: 'RestaurantID', nameField: 'RestaurantName',
    category: 'Tourist restaurant', icon: 'R', color: '#2b8c6e', max: 300 },
  { path: 'Event', idField: 'EventID', nameField: 'EventName',
    category: 'Cultural and creative activities', icon: 'A', color: '#9e2a1c', max: 200, isEvent: true },
];

const PAGE_SIZE = 500;          // TDX Tourism 的 $top 上限
const REQUEST_GAP_MS = 1500;    // 每次請求間隔
const MAX_RETRIES = 6;
const RAW_LIMIT = 3000;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const clean = s => String(s ?? '').replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();

async function getToken() {
  const res = await fetch(
    'https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'client_credentials',
        client_id: TDX_CLIENT_ID,
        client_secret: TDX_CLIENT_SECRET,
      }),
    }
  );
  if (!res.ok) throw new Error('Token HTTP ' + res.status);
  const data = await res.json();
  if (!data.access_token) throw new Error('No access_token');
  return data.access_token;
}

// 遇到 429 / 5xx 會自動等待重試
async function fetchPage(src, token, top, skip) {
  const url = `${BASE}/${src.path}?$top=${top}&$skip=${skip}&$format=JSON`;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const res = await fetch(url, { headers: { authorization: `Bearer ${token}` } });

    if (res.ok) {
      const json = await res.json();
      // 有些端點直接回傳陣列，有些包在 value 裡
      const items = Array.isArray(json) ? json : (Array.isArray(json.value) ? json.value : []);
      return { ok: true, items };
    }

    if (res.status === 429 || res.status >= 500) {
      const retryAfter = Number(res.headers.get('retry-after'));
      const wait = retryAfter > 0
        ? retryAfter * 1000
        : Math.min(60000, 3000 * 2 ** attempt); // 3s, 6s, 12s, 24s, 48s, 60s
      console.log(`${src.path}: HTTP ${res.status} (skip=${skip}), retry ${attempt + 1}/${MAX_RETRIES} in ${wait / 1000}s`);
      await sleep(wait);
      continue;
    }

    // 其他錯誤（例如 400）不重試
    const body = (await res.text()).slice(0, 300);
    return { ok: false, status: res.status, body };
  }

  return { ok: false, status: 429, body: 'retries exhausted' };
}

async function fetchAll(src, token) {
  const seen = new Set();
  const all = [];
  // 轉換後會被過濾掉一部分，所以多抓一些再截斷
  const target = Math.min(RAW_LIMIT, src.max * 2);

  let skip = 0;
  while (all.length < target) {
    const r = await fetchPage(src, token, PAGE_SIZE, skip);
    if (!r.ok) {
      console.log(`${src.path}: skip=${skip} failed -> HTTP ${r.status} ${r.body}`);
      break; // 保留已經抓到的資料
    }
    if (r.items.length === 0) break;

    let added = 0;
    for (const it of r.items) {
      const id = it[src.idField];
      if (id && !seen.has(id)) { seen.add(id); all.push(it); added++; }
    }
    if (added === 0 || r.items.length < PAGE_SIZE) break;

    skip += r.items.length;
    await sleep(REQUEST_GAP_MS);
  }
  return all;
}

function convert(src, item) {
  const lat = parseFloat(item.PositionLat ?? item.Position?.PositionLat);
  const lng = parseFloat(item.PositionLon ?? item.Position?.PositionLon);
  if (isNaN(lat) || isNaN(lng)) return null;
  if (lat < 21.5 || lat > 26.5 || lng < 118.0 || lng > 122.5) return null;

  const name = clean(item[src.nameField]);
  if (!name) return null;

  if (src.isEvent) {
    if (item.EventStatus === 'EventCancelled') return null;
    const end = item.EndDateTime ? Date.parse(item.EndDateTime) : NaN;
    if (!isNaN(end) && end < Date.now()) return null;
  }

  const a = item.PostalAddress || {};
  const addr = [a.City, a.Town, a.StreetAddress].filter(Boolean).join('') || item.Address || '';

  return {
    id: item[src.idField],
    name,
    category: src.category,
    icon: src.icon,
    color: src.color,
    lat, lng, addr,
    desc: clean(item.Description).slice(0, 200),
    pic: item.Images?.[0]?.URL || item.Picture?.PictureUrl1 || '',
    officialUrl: item.WebsiteUrl || 'https://www.taiwan.net.tw/',
    mapUrl: `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(name + ' ' + addr)}`,
  };
}

async function loadPrevious() {
  try {
    const arr = JSON.parse(await readFile('data/locations.json', 'utf8'));
    return Array.isArray(arr) ? arr : [];
  } catch { return []; }
}

async function main() {
  const token = await getToken();
  const previous = await loadPrevious();
  const output = [];

  for (const src of SOURCES) {
    let locs = [];
    try {
      const raw = await fetchAll(src, token);
      locs = raw.map(it => convert(src, it)).filter(Boolean)
        .sort((a, b) => a.id.localeCompare(b.id))
        .slice(0, src.max);
      console.log(`${src.path}: raw ${raw.length} -> kept ${locs.length}`);
    } catch (e) {
      console.log(`${src.path}: error ${e.message}`);
    }

    // 這個來源抓不到時，沿用上次的舊資料，避免整份資料壞掉
    if (locs.length === 0) {
      locs = previous.filter(p => p.category === src.category);
      console.log(`${src.path}: fallback to previous data (${locs.length})`);
    }
    output.push(...locs);

    await sleep(REQUEST_GAP_MS * 2); // 來源之間多休息一下
  }

  if (output.length === 0) throw new Error('No data at all, abort');

  await mkdir('data', { recursive: true });
  await writeFile('data/locations.json', JSON.stringify(output));
  console.log(`Wrote ${output.length} locations`);
}

main().catch(err => {
  console.error('FAILED:', err.message);
  process.exit(1);
});