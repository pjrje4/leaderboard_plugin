'''
leaderboard by Dr. Jekyll
1.0 Release

Description: shows Eschaton Eye all-time callout leaderboard ranks in game
(https://ee.wrya.net/leaderboards/all-time.html).
- name tags: players on the leaderboard get their rank after their name,
  "[6] Jekyll [2403]". players not on it and new sparks are left alone.
- faction roster: the rank after each member's name.
- Tab (STATUS box): your own leaderboard rank and points on the top two rows.
- callouts in chat add that zone's reward to the player, so ranks stay current
  through the day.

How it works: the leaderboard page is downloaded in the background at most once
a day (on game start if today's copy isn't there yet). it is saved to
leaderboard_cache.json in the SBPE folder; callouts seen since the download are
kept in leaderboard_mirror.json so a restart the same day doesn't lose them. a
failed download is retried every 30 minutes.

Install: drop this file into SBPE's plugins folder.

If you want to customize it paste this into config.ini:

[plugin_leaderboard]
enabled = yes
# rank after player name tags
name_tags = yes
# rank after names in the faction roster
roster = yes
# your rank and points in the STATUS box (Tab)
tab_lines = yes
# decimals of your points
decimals = 2
'''

import bisect
import datetime
import html
import json
import logging
import os
import re
import struct
import threading
import time
import urllib.request

import cffi

from _remote import ffi
from manager import PluginBase
import util

URL = 'https://ee.wrya.net/leaderboards/all-time.html'
CACHE_FILE = 'leaderboard_cache.json'
MIRROR_FILE = 'leaderboard_mirror.json'
RETRY_S = 30 * 60
MICRO = 1000000                 # points are integer millionths (the page has 6 decimals)
NEW_SPARK = 'A New Spark'       # the page's shared row for all new sparks

CALLOUT = re.compile(r'The (.+?) has been defeated!\s+(.+?) gave an impressive performance')
REWARD_RE = re.compile(r'<div><b>([^<]*)</b></div><div>([\d.]+)</div>')
ROW_RE = re.compile(r'<tr><td>(\d+)</td><td>([^<]*)</td><td>([\d .]+)</td></tr>')
GENERATED_RE = re.compile(r'<th>Generated on</th><td>([^<]*)</td>')

# game functions: address, first bytes. if a game update moves them the bytes
# differ and that feature turns itself off instead of crashing
FUNCS = {
    'WorldView::getNameTexture': (0x4d34b0, '5589e55381ec44010000'),
    'TextTexture::setText': (0x47b890, '5589e557565383ec1c8d'),
    'Label::Label': (0x461120, '5589e583ec788d55f889'),
    'Label::setText': (0x4612c0, '5589e557565383ec1c8d'),
    'UIElementContainer::addChild': (0x467b40, '5589e583ec688d45f889'),
    'ToolTip::calculateH': (0x4bfc60, '5589e55389cb83ec148b'),
}
NEW = 0x637a30                  # operator new
LABEL_SIZE = 0x54

# STATUS box (StatsWindow): TenbyFive 14, keys at x=28, values at x=142, 20 px rows
ROW_H = 20
KEY_X, VAL_X = 28, 142
FONT = b'TenbyFive'
WHITE, BLACK = 0xffffffff, 0xff000000
ROSTER_NAME_X = 26              # x of the name Label in a faction roster line
ROSTER_GAP = 6                  # px between a roster name and its rank


# calling the game's __thiscall methods from cffi through a small thunk
_kffi = cffi.FFI()
_kffi.cdef('void * __stdcall VirtualAlloc(void *, size_t, unsigned long, unsigned long);')
_k32 = _kffi.dlopen('kernel32.dll')


class ExecMemory(object):
    def __init__(self, size=4096):
        p = _k32.VirtualAlloc(_kffi.NULL, size, 0x3000, 0x40)   # commit+reserve, rwx
        if p == _kffi.NULL:
            raise MemoryError('VirtualAlloc failed')
        self.page = p
        self.base = int(_kffi.cast('uintptr_t', p))
        self.used = 0

    def put(self, code):
        a = self.base + self.used
        _kffi.memmove(_kffi.cast('char *', a), code, len(code))
        self.used += (len(code) + 15) & ~15
        return a


def thunk_cdecl_to_thiscall(target, nargs):
    '''cdecl f(this, arg1..argN) -> __thiscall target'''
    code = b'\x55\x89\xe5'                                      # push ebp; mov ebp, esp
    for i in reversed(range(nargs)):
        code += b'\xff\x75' + bytes([12 + 4 * i])               # push [ebp+12+4i]
    code += b'\x8b\x4d\x08'                                     # mov ecx, [ebp+8]
    code += b'\xb8' + struct.pack('<I', target) + b'\xff\xd0'   # call target
    code += b'\x89\xec\x5d\xc3'                                 # mov esp, ebp; pop ebp; ret
    return code


def func_ok(name, hooked_ok=False):
    a, start = FUNCS[name]
    have = bytes(ffi.buffer(ffi.cast('char *', a), len(start) // 2)).hex()
    # another plugin may have hooked it (a jmp at the start)
    return have == start or (hooked_ok and have.startswith('e9'))


def addr(p):
    return int(ffi.cast('uintptr_t', p))


def today():
    return datetime.date.today().isoformat()


def micro(s):
    return int(round(float(s.replace(' ', '')) * MICRO))


# leaderboard page ###########################################################

def parse_page(text):
    rewards = {html.unescape(z).strip(): micro(v) for z, v in REWARD_RE.findall(text)}
    i = max(text.find('leaderboard-table'), 0)
    players = [[html.unescape(n), micro(p)] for _, n, p in ROW_RE.findall(text, i)]
    if not rewards or len(players) < 100:
        raise ValueError('page not understood ({} rewards, {} players)'.format(len(rewards), len(players)))
    g = GENERATED_RE.search(text)
    return {'generated': g.group(1) if g else '', 'rewards': rewards, 'players': players}


def fetch_worker(job, path):
    '''background thread: download, parse, save'''
    try:
        req = urllib.request.Request(URL, headers={'User-Agent': 'sbpe-leaderboard'})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode('utf-8', errors='replace')
        data = parse_page(text)
        data['date'] = today()
        with open(path + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(data, f, separators=(',', ':'))
        os.replace(path + '.tmp', path)
        job['result'] = data
    except Exception as e:
        job['error'] = repr(e)
    job['done'] = True


class Board(object):
    '''every player's points, sorted; ties keep page order'''
    def __init__(self, data):
        self.rewards = data['rewards']
        self.generated = data.get('generated', '')
        self.date = data.get('date', '')
        self.pts = {}       # name -> [points, tie order]
        self.lower = {}     # lowercase name -> name
        for i, (n, p) in enumerate(data['players']):
            self.pts[n] = [p, i]
            self.lower.setdefault(n.lower(), n)
        self.keys = sorted((-p, o) for p, o in self.pts.values())
        self.nextOrder = len(data['players'])
        self.ranks = {}     # name -> rank cache, cleared on every change

    def find(self, name):
        return name if name in self.pts else self.lower.get(name.lower())

    def rank(self, name):
        n = self.find(name)
        if n is None:
            return None
        r = self.ranks.get(n)
        if r is None:
            p, o = self.pts[n]
            r = self.ranks[n] = bisect.bisect_left(self.keys, (-p, o)) + 1
        return r

    def points(self, name):
        n = self.find(name)
        return None if n is None else self.pts[n][0]

    def add(self, name, amount):
        '''-> (old rank or None, new rank)'''
        n = self.find(name)
        old = None
        if n is None:
            n = name
            self.pts[n] = [0, self.nextOrder]
            self.lower.setdefault(n.lower(), n)
            self.nextOrder += 1
        else:
            old = self.rank(n)
            p, o = self.pts[n]
            i = bisect.bisect_left(self.keys, (-p, o))
            if i < len(self.keys) and self.keys[i] == (-p, o):
                del self.keys[i]
        e = self.pts[n]
        e[0] += amount
        bisect.insort(self.keys, (-e[0], e[1]))
        self.ranks = {}
        return old, self.rank(n)


# plugin #####################################################################

class Plugin(PluginBase):
    def onInit(self):
        self.config.option('name_tags', True, 'bool')
        self.config.option('roster', True, 'bool')
        self.config.option('tab_lines', True, 'bool')
        self.config.option('decimals', 2, 'int')

        self.cachePath = os.path.join(self.refs.SCRIPTPATH, CACHE_FILE)
        self.mirrorPath = os.path.join(self.refs.SCRIPTPATH, MIRROR_FILE)
        self.board = None
        self.mirror = {'generated': None, 'callouts': []}
        self.lastTry = 0
        self.dateCheckT = 0
        self.chatLast = None    # (entry address, receiveTime) of the last chat line read
        self.tags = {}          # name texture address -> (game's text, our text)
        self.wv = 0
        self.sw = None          # (StatsWindow address, key labels, value labels)
        self.rosterLabels = {}  # roster line address -> rank Label
        self.errors = 0
        self.tagsOk = self.uiOk = False
        self.setupNative()
        self.load()

    def setupNative(self):
        fst = self.refs.get('factionColorHook')     # set by a plugin that hooks name textures
        hooked = bool(fst and fst.get('ok'))
        self.mem = ExecMemory()

        def tc(name, n, sig):
            return ffi.cast(sig, self.mem.put(thunk_cdecl_to_thiscall(FUNCS[name][0], n)))

        if func_ok('WorldView::getNameTexture', hooked) and func_ok('TextTexture::setText'):
            self.getNameTexture = tc('WorldView::getNameTexture', 1, 'void *(*)(void *, void *)')
            self.texSetText = tc('TextTexture::setText', 1, 'void(*)(void *, char *)')
            self.tagsOk = True
        else:
            logging.error('leaderboard: game functions not found (game updated?), name tags off')
        if all(func_ok(n) for n in ('Label::Label', 'Label::setText', 'UIElementContainer::addChild',
                                    'ToolTip::calculateH')):
            self.newObj = ffi.cast('void *(*)(unsigned int)', NEW)
            self.labelCtor = tc('Label::Label', 7, 'void(*)(void *, char *, char *, int, uint32_t, int, uint32_t, int)')
            self.labelSetText = tc('Label::setText', 1, 'void(*)(void *, char *)')
            self.addChild = tc('UIElementContainer::addChild', 1, 'void(*)(void *, void *)')
            self.calculateH = tc('ToolTip::calculateH', 0, 'void(*)(void *)')
            self.uiOk = True
        else:
            logging.error('leaderboard: game functions not found (game updated?), status/roster off')

    # data ###################################################################

    def load(self):
        try:
            with open(self.cachePath, encoding='utf-8') as f:
                self.setBoard(json.load(f), fresh=False)
        except FileNotFoundError:
            pass
        except Exception:
            logging.exception('leaderboard: bad cache, downloading again')
        job = self.refs.get('leaderboardFetch')
        if job and not job.get('done'):
            return  # still downloading from before a plugin reload
        if self.board is None or self.board.date != today():
            self.startFetch()

    def setBoard(self, data, fresh):
        self.board = Board(data)
        try:
            with open(self.mirrorPath, encoding='utf-8') as f:
                m = json.load(f)
        except Exception:
            m = None
        if fresh or not m or m.get('generated') != self.board.generated:
            # a new page already includes every earlier callout
            self.mirror = {'generated': self.board.generated, 'callouts': []}
            self.saveMirror()
        else:
            self.mirror = m
            for name, zone, amount in m['callouts']:
                self.board.add(name, amount)
        logging.info('leaderboard: {} players, page of {}, {} callouts since'.format(
            len(self.board.pts), self.board.generated, len(self.mirror['callouts'])))

    def saveMirror(self):
        try:
            with open(self.mirrorPath + '.tmp', 'w', encoding='utf-8') as f:
                json.dump(self.mirror, f)
            os.replace(self.mirrorPath + '.tmp', self.mirrorPath)
        except Exception:
            logging.exception('leaderboard: cannot save ' + MIRROR_FILE)

    def startFetch(self):
        self.lastTry = time.time()
        job = self.refs['leaderboardFetch'] = {'done': False}
        threading.Thread(target=fetch_worker, args=(job, self.cachePath), daemon=True).start()
        logging.info('leaderboard: downloading ' + URL)

    def pollFetch(self):
        job = self.refs.get('leaderboardFetch')
        if job and job.get('done') and not job.get('used'):
            job['used'] = True
            if 'result' in job:
                self.setBoard(job['result'], fresh=True)
            else:
                logging.warning('leaderboard: download failed: {}'.format(job.get('error')))
        now = time.time()
        if now - self.dateCheckT < 60:
            return
        self.dateCheckT = now
        busy = job is not None and not job.get('done')
        if not busy and (self.board is None or self.board.date != today()) and now - self.lastTry > RETRY_S:
            self.startFetch()

    # chat ###################################################################

    def readChat(self):
        gc = self.refs.GameClient
        if gc == ffi.NULL or gc.chatWindow == ffi.NULL or gc.chatWindow.chatLog == ffi.NULL:
            return
        entries = util.vec2list(gc.chatWindow.chatLog.entries, 'struct ChatLogEntry *')
        if not entries:
            return
        last = (addr(entries[-1]), entries[-1].receiveTime)
        if self.chatLast is None or last == self.chatLast:
            self.chatLast = last    # first look: skip what was already there
            return
        start = len(entries) - 1    # lost track: only the newest line
        for i in range(len(entries) - 1, -1, -1):
            if (addr(entries[i]), entries[i].receiveTime) == self.chatLast:
                start = i + 1
                break
        self.chatLast = last
        for e in entries[start:]:
            if e != ffi.NULL and e.chat.message != ffi.NULL:
                self.onChat(util.getstr(e.chat.message))

    def onChat(self, text):
        m = CALLOUT.search(text)
        if not m or self.board is None:
            return
        zone, who = m.group(1).strip(), m.group(2).strip()
        # "<name>" or "<name> of <faction>"; names have no spaces
        name = NEW_SPARK if who.startswith(NEW_SPARK) else who.split(' ', 1)[0]
        amount = self.board.rewards.get(zone)
        if not amount:
            return  # no reward for this zone
        old, new = self.board.add(name, amount)
        self.mirror['callouts'].append([name, zone, amount])
        self.saveMirror()
        logging.info('leaderboard: {} +{:.6f} ({}) rank {} -> {}'.format(name, amount / MICRO, zone, old, new))

    # name tags ##############################################################

    def updateTags(self):
        wc, cw = self.refs.WorldClient, self.refs.ClientWorld
        if wc == ffi.NULL or cw == ffi.NULL or wc.worldView == ffi.NULL:
            return
        if addr(wc.worldView) != self.wv:
            self.wv = addr(wc.worldView)
            self.tags = {}      # new map, new texture cache
        objs = util.vec2list(cw.allies, 'struct WorldObject *')
        if cw.player != ffi.NULL:
            objs.append(ffi.cast('struct WorldObject *', cw.player))
        fst = self.refs.get('factionColorHook')
        for obj in objs:
            if obj == ffi.NULL or obj.props.displayname == ffi.NULL:
                continue
            name = ffi.string(obj.props.displayname.s, 100).decode('utf-8', 'replace')
            # the game's cached name tag texture, "[level] Name"
            tex = self.getNameTexture(wc.worldView, obj)
            if tex == ffi.NULL:
                continue
            t = ffi.cast('struct TextTexture *', tex)
            a = addr(tex)
            cur = ffi.string(t.text.s, 200)
            prev = self.tags.get(a)
            base = prev[0] if prev and prev[1] == cur else cur
            r = self.board.rank(name) if name and self.config.name_tags else None
            want = base + (b'' if r is None else ' [{}]'.format(r).encode())
            if want != cur:
                self.texSetText(tex, ffi.new('char[]', want))
                if fst and a in fst.get('changed', {}):
                    # keep that plugin's record of the texture's text buffer current
                    fst['changed'][a] = (fst['changed'][a][0], addr(t.text.s))
            self.tags[a] = (base, want)

    # STATUS box (Tab) #######################################################

    def label(self, parent, text, x, y):
        lb = self.newObj(LABEL_SIZE)
        self.labelCtor(lb, ffi.new('char[]', text), ffi.new('char[]', FONT), 14, WHITE, 2, BLACK, 0)
        e = ffi.cast('struct UIElement *', lb)
        e.x, e.y = x, y
        self.addChild(parent, lb)
        return lb

    def myName(self):
        cw = self.refs.ClientWorld
        if cw == ffi.NULL or cw.player == ffi.NULL:
            return None
        dn = ffi.cast('struct WorldObject *', cw.player).props.displayname
        return (ffi.string(dn.s, 100).decode('utf-8', 'replace') or None) if dn != ffi.NULL else None

    def statusTexts(self):
        if self.board is None:
            job = self.refs.get('leaderboardFetch')
            v = b'downloading...' if job is not None and not job.get('done') else b'not available'
            return v, v
        me = self.myName()
        if me is None:
            return b'new spark', b'-'
        r = self.board.rank(me)
        if r is None:
            return b'not ranked', b'0'
        p = self.board.points(me) / MICRO
        return '#{:,}'.format(r).encode(), '{:,.{}f}'.format(p, max(0, self.config.decimals)).encode()

    def updateStatus(self):
        wc = self.refs.WorldClient
        if wc == ffi.NULL or wc.overlay == ffi.NULL or util.getClassName(wc.overlay) != 'InventoryOverlay':
            return
        sw = ffi.cast('struct InventoryOverlay *', wc.overlay).statsWindow
        if sw == ffi.NULL or sw.keys[0] == ffi.NULL:
            return
        kids = util.vec2list(ffi.cast('struct UIElementContainer *', sw).children, 'struct UIElement *')
        a = addr(sw)
        if self.sw is None or self.sw[0] != a or addr(self.sw[2][0]) not in {addr(k) for k in kids}:
            if not self.config.tab_lines:
                return
            # move the stat rows down two rows and put ours on top
            top = ffi.cast('struct UIElement *', sw.keys[0]).y
            bg = addr(sw.asToolTip.background)
            for k in kids:
                if addr(k) != bg and k.y >= top:
                    k.y += 2 * ROW_H
            rank, pts = self.statusTexts()
            keys = [self.label(sw, b'Leaderboard Rank', KEY_X, top),
                    self.label(sw, b'Leaderboard Points', KEY_X, top + ROW_H)]
            vx = max([VAL_X] + [ffi.cast('struct UIElement *', k).w + KEY_X + 8 for k in keys])
            vals = [self.label(sw, rank, vx, top), self.label(sw, pts, vx, top + ROW_H)]
            sw.asToolTip.hPos += 2 * ROW_H
            self.calculateH(sw)     # grow the box
            self.sw = (a, keys, vals)
            return
        for lb, s in zip(self.sw[2], self.statusTexts()):
            self.labelSetText(lb, ffi.new('char[]', s))

    # faction roster #########################################################

    def updateRoster(self):
        wc = self.refs.WorldClient
        if wc == ffi.NULL or wc.overlay == ffi.NULL or util.getClassName(wc.overlay) != 'FactionRosterWindow':
            self.rosterLabels = {}
            return
        dlg = ffi.cast('struct FactionRosterWindow *', wc.overlay).factionRosterDialog
        if dlg == ffi.NULL:
            return      # roster not back from the server yet
        seen = {}
        for line in util.vec2list(dlg.lines, 'struct UIElementContainer *'):
            if line == ffi.NULL:
                continue
            kids = util.vec2list(line.children, 'struct UIElement *')
            names = [k for k in kids if k.x == ROSTER_NAME_X]
            if not names:
                continue
            ne = names[0]
            nl = ffi.cast('struct Label *', ne)
            name = ffi.string(nl.text.s, 100).decode('utf-8', 'replace')
            r = self.board.rank(name) if self.board is not None and self.config.roster else None
            text = b'' if r is None else '[{}]'.format(r).encode()
            la = addr(line)
            lb = self.rosterLabels.get(la)
            if lb is not None and addr(lb) not in {addr(k) for k in kids}:
                lb = None   # a new line at a reused address
            if lb is None:
                if not text:
                    continue
                # a separate Label after the name, in the name's font and color
                lb = self.newObj(LABEL_SIZE)
                font = ffi.new('char[]', ffi.string(nl.fontName.s, 100) or FONT)
                self.labelCtor(lb, ffi.new('char[]', text), font,
                               nl.fontSize, nl.color, nl.outlineSize, nl.outlineColor, 0)
                self.addChild(line, lb)
            else:
                self.labelSetText(lb, ffi.new('char[]', text))
            e = ffi.cast('struct UIElement *', lb)
            e.x, e.y = ne.x + ne.w + ROSTER_GAP, ne.y
            seen[la] = lb
        self.rosterLabels = seen

    # callbacks ##############################################################

    def afterUpdate(self):
        try:
            self.pollFetch()
            self.readChat()
            if self.tagsOk and self.board is not None:
                self.updateTags()
            if self.uiOk:
                self.updateStatus()
                self.updateRoster()
        except Exception:
            self.errors += 1
            if self.errors <= 5:
                logging.exception('leaderboard: update failed')
