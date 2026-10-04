import { mkdir, writeFile } from 'node:fs/promises';

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

const PAGE = 1000;
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

async function fetchAll(src, token) {
  const seen = new Set();
  const all = [];
  let skip = 0;
  while (all.length < RAW_LIMIT) {
    const url = `${BASE}/${src.path}?$top=${PAGE}&$skip=${skip}`;
    const res = await fetch(url, { headers: { authorization: `Bearer ${token}` } });
    if (!res.ok) throw new Error(`${src.path} HTTP ${res.status}`);
    const json = await res.json();
    const items = Array.isArray(json.value) ? json.value : [];
    if (items.length === 0) break;
    let added = 0;
    for (const it of items) {
      const id = it[src.idField];
      if (id && !seen.has(id)) { seen.add(id); all.push(it); added++; }
    }
    if (added === 0) break;           // 伺服器沒有新資料（例如忽略 $skip），避免無限迴圈
    skip += items.length;
    await sleep(300);
  }
  return all;
}

function convert(src, item) {
  const lat = parseFloat(item.PositionLat);
  const lng = parseFloat(item.PositionLon);
  if (isNaN(lat) || isNaN(lng)) return null;
  if (lat < 21.5 || lat > 26.5 || lng < 118.0 || lng > 122.5) return null;

  const name = clean(item[src.nameField]);
  if (!name) return null;

  if (src.isEvent) {
    if (item.EventStatus === 'EventCancelled') return null;
    const end = item.EndDateTime ? Date.parse(item.EndDateTime) : NaN;
    if (!isNaN(end) && end < Date.now()) return null;   // 略過已結束的活動
  }

  const a = item.PostalAddress || {};
  const addr = [a.City, a.Town, a.StreetAddress].filter(Boolean).join('');

  return {
    id: item[src.idField],
    name,
    category: src.category,
    icon: src.icon,
    color: src.color,
    lat, lng, addr,
    desc: clean(item.Description).slice(0, 200),
    pic: item.Images?.[0]?.URL || '',
    officialUrl: item.WebsiteUrl || 'https://www.taiwan.net.tw/',
    mapUrl: `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(name + ' ' + addr)}`,
  };
}

const token = await getToken();
const output = [];

for (const src of SOURCES) {
  const raw = await fetchAll(src, token);
  const locs = raw.map(it => convert(src, it)).filter(Boolean)
    .sort((a, b) => a.id.localeCompare(b.id))
    .slice(0, src.max);
  console.log(`${src.path}: raw ${raw.length} -> kept ${locs.length}`);
  if (locs.length === 0) throw new Error(`${src.path} produced 0 locations, abort`);
  output.push(...locs);
}

await mkdir('data', { recursive: true });
await writeFile('data/locations.json', JSON.stringify(output));
console.log(`Wrote ${output.length} locations`);