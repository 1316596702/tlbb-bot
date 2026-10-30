"""Leader-only chat settings and the captured normal-client CGChat layout."""
import json
import struct
import tempfile
from pathlib import Path

FILE_NAME = 'leader_shout.json'
CHANNELS = {'local':0,'team':1,'world':2,'guild':6,'sect':7,'horn':10,'city':11}
DEFAULT = dict(enabled=False, leader='', text='', channel='world', interval_seconds=60)


def validate_settings(data, leader=None):
    if not isinstance(data, dict):raise ValueError('喊话设置格式无效')
    enabled=data.get('enabled',False)
    if type(enabled) is not bool:raise ValueError('启用状态无效')
    channel=data.get('channel','world')
    if not isinstance(channel,str) or channel not in CHANNELS:raise ValueError('请选择有效的喊话频道')
    interval=data.get('interval_seconds',60)
    if type(interval) is not int or not 1<=interval<=86400:
        raise ValueError('喊话间隔须为1至86400之间的整数秒')
    text=data.get('text','')
    if not isinstance(text,str):raise ValueError('喊话内容无效')
    text=text.strip()
    if any(ord(c)<32 or ord(c)==127 for c in text):raise ValueError('喊话内容请填写一行文字')
    try:encoded=text.encode('gbk')
    except UnicodeEncodeError:raise ValueError('内容包含游戏不支持的表情或特殊字符，请替换') from None
    if len(encoded)>319:raise ValueError('喊话内容过长，最多159个汉字或319个英文字符')
    account=data.get('leader','') if leader is None else leader
    if not isinstance(account,str):raise ValueError('喊话队长无效')
    if enabled and (not text or not account):raise ValueError('请先设置刷马队长并填写喊话内容')
    return dict(enabled=enabled,leader=account,text=text,channel=channel,interval_seconds=interval)


def load_all_settings(base):
    path=Path(base)/FILE_NAME
    if not path.exists():return {}
    document=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document,dict):raise ValueError('喊话设置格式无效')
    if document.get('format')==2:
        leaders=document.get('leaders')
        if not isinstance(leaders,dict):raise ValueError('队长喊话列表无效')
        result={}
        for account,settings in leaders.items():
            if not isinstance(account,str) or not account:raise ValueError('喊话队长无效')
            validated=validate_settings(settings,leader=account)
            result[account]=validated
        return result
    legacy=validate_settings(document)
    return {legacy['leader']:legacy} if legacy['leader'] else {}


def load_settings(base,leader=None):
    leaders=load_all_settings(base)
    if leader is None:
        if len(leaders)>1:raise ValueError('请指定喊话队长')
        return next(iter(leaders.values()),dict(DEFAULT))
    return leaders.get(leader,dict(DEFAULT,leader=leader))


def save_settings(base,data):
    data=validate_settings(data)
    if not data['leader']:
        raise ValueError('请指定喊话队长')
    leaders=load_all_settings(base);leaders[data['leader']]=data
    base=Path(base);path=base/FILE_NAME
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=base,
                                     prefix='leader_shout_',suffix='.tmp',delete=False) as output:
        json.dump(dict(format=2,leaders=leaders),output,ensure_ascii=False,indent=2);temporary=Path(output.name)
    try:temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def chat_body(channel,text,context=None):
    settings=validate_settings(dict(enabled=True,leader='verified',channel=channel,
                                    text=text,interval_seconds=60))
    encoded=settings['text'].encode('gbk')
    context=context or {}
    extra=b''
    if channel=='local':extra=struct.pack('<H',0)
    elif channel in ('team','guild'):
        value=context.get(channel)
        if type(value) is not int or not (0 if channel=='team' else 1)<=value<65535:
            raise ValueError('角色当前没有可用的队伍或帮派频道')
        extra=struct.pack('<H',value)
    elif channel=='sect':
        value=context.get('sect')
        if type(value) is not int or not 0<=value<255:raise ValueError('角色当前没有可用的门派频道')
        extra=bytes([value])
    elif channel=='city':extra=struct.pack('<I',1) # Captured default 同城 selection.
    prefix=bytes([CHANNELS[channel]])+struct.pack('<I',len(encoded))+encoded
    return prefix+bytes.fromhex('00ffffffff00ffffffff')+extra+b'\0\0\0'
