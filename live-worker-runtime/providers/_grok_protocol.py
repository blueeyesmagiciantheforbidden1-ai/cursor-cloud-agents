"""Bounded native ACP transport. No raw credential/profile/diagnostic logging."""
import json, os, queue, signal, subprocess, threading, time
from pathlib import Path

NATIVE='/opt/runcrew/grok/grok'
MAX_MESSAGE=2*1024*1024
class NativeError(ValueError): pass
class NativeStartupStopped(NativeError):
    """Startup failed, with no spawned process left running."""
class NativeStopUncertain(NativeError):
    """A spawned process could not be confirmed stopped; quarantine the lease."""
def need(value, reason):
    if not value: raise NativeError(reason)

def frame_envelope(value,expected_id):
    """Only fixed keys, scalar type names and correlation categories; no payload/IDs."""
    known=('jsonrpc','id','method','params','result','error')
    def kind(item):
        return {type(None):'null',bool:'boolean',int:'integer',float:'number',str:'string',dict:'object',list:'array'}.get(type(item),'other')
    observed=value.get('id')
    correlation=('missing'if'id'not in value else'type_mismatch'if type(observed)is not type(expected_id)
      else'matches'if observed==expected_id else'different')
    return {'known_fields':{key:kind(value[key])for key in known if key in value},
      'unknown_field_count':sum(key not in known for key in value),
      'expected_id_type':kind(expected_id),'observed_id_type':kind(observed)if'id'in value else'missing',
      'id_correlation':correlation}

def internal_reload_ack(value):
    """Recognize only exact successful replies to Grok's own stdio watcher.

    The native watcher injects these fixed request IDs into its own input stream.
    They are bookkeeping acknowledgments, never a response to our model prompt.
    This isolated native process has at most one resident session.
    """
    ident=value.get('id')
    if type(ident)is not str or ident not in ('skills-reload','workflows-reload'):return None
    outer=value.get('result')
    inner=outer.get('result')if type(outer)is dict else None
    need(set(value)=={'jsonrpc','id','result'} and value.get('jsonrpc')=='2.0'
         and type(outer)is dict and set(outer)=={'result'}
         and type(inner)is dict and set(inner)=={'reloaded'}
         and type(inner.get('reloaded'))is int and 0<=inner['reloaded']<=1,
         'native_internal_reload_schema')
    return ident

def environment(home):
    env={'HOME':str(home),'GROK_HOME':str(home/'.grok'),'PATH':'/usr/local/bin:/usr/bin:/bin',
        'LANG':'C.UTF-8','TMPDIR':str(home/'tmp'),'GROK_DISABLE_AUTOUPDATER':'1',
        'GROK_CRASH_HANDLER':'0','GROK_SUBAGENTS':'0','GROK_MEMORY':'0','NO_COLOR':'1','TERM':'dumb'}
    for family in ('CLAUDE','CURSOR'):
        for feature in ('SKILLS','RULES','AGENTS','MCPS','HOOKS'):
            env[f'GROK_{family}_{feature}_ENABLED']='0'
    return env

class Native:
    def __init__(self,home,renew,deadline):
        self.home,self.renew,self.deadline=Path(home),renew,deadline
        self.next_renew=time.monotonic()+20;self.sequence=0;self.events=queue.Queue(maxsize=256)
        self.notifications=[];self.prompt_sent=False;self.closed=False;self.internal_ack_counts={}
        try:
            self.process=subprocess.Popen([NATIVE,'agent','--no-leader','stdio'],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                cwd=home/'work',env=environment(home),shell=False,start_new_session=True,bufsize=0)
        except Exception as failure:
            # Popen cleans up a child when process creation itself fails.
            raise NativeStartupStopped('native_process_start_failed') from failure
        def reader():
            try:
                while True:
                    raw=self.process.stdout.readline(MAX_MESSAGE+1)
                    if not raw: break
                    if len(raw)>MAX_MESSAGE: self.events.put(('error','native_message_limit'));return
                    self.events.put(('message',json.loads(raw)))
            except Exception: self.events.put(('error','native_protocol_decode'))
            finally:self.events.put(('eof',None))
        try:
            self.reader=threading.Thread(target=reader,daemon=True);self.reader.start()
        except BaseException as failure:
            try:self.close()
            except BaseException as stop_failure:
                raise NativeStopUncertain('native_startup_stop_uncertain') from stop_failure
            raise NativeStartupStopped('native_reader_start_failed') from failure
    def send(self,value):
        raw=(json.dumps(value,separators=(',',':'))+'\n').encode()
        need(len(raw)<=MAX_MESSAGE,'request_limit')
        # Unbuffered pipe writes may be short. Continue only the unwritten suffix,
        # never resend a whole request whose prefix could already be accepted.
        remaining=memoryview(raw)
        while remaining:
            need(time.monotonic()<self.deadline,'native_deadline')
            try:count=self.process.stdin.write(remaining)
            except InterruptedError:continue
            except (OSError,ValueError)as failure:
                raise NativeError('native_write_failed')from failure
            need(type(count)is int and 0<count<=len(remaining),'native_write_invalid_count')
            remaining=remaining[count:]
        try:self.process.stdin.flush()
        except (OSError,ValueError)as failure:
            raise NativeError('native_flush_failed')from failure
    def request(self,method,params):
        allowed={'initialize','authenticate','x.ai/auth/info','x.ai/auth/check_subscription','x.ai/billing',
                 'x.ai/auto-topup-rule','x.ai/models/list','session/new','session/set_model',
                 'session/set_config_option','x.ai/session/info','session/prompt'}
        need(method in allowed,'method_forbidden')
        if method=='session/prompt':
            need(not self.prompt_sent,'prompt_replay_forbidden');self.prompt_sent=True
        # Use one stable JSON-RPC ID type across metadata and inference calls.
        # Response IDs must still match exactly; never coerce received IDs.
        self.sequence+=1;ident=str(self.sequence)
        # ACP custom methods use an underscore on the wire; the official Rust
        # handler receives the prefix-stripped x.ai/... name.
        wire_method='_'+method if method.startswith('x.ai/') else method
        self.send({'jsonrpc':'2.0','id':ident,'method':wire_method,'params':params})
        while True:
            now=time.monotonic();need(now<self.deadline,'native_deadline')
            if now>=self.next_renew:self.renew();self.next_renew=time.monotonic()+20
            try:kind,item=self.events.get(timeout=.25)
            except queue.Empty:continue
            need(kind=='message','native_'+kind)
            need(isinstance(item,dict),'native_object_required')
            if 'method' in item:
                if 'id' in item:
                    self.send({'jsonrpc':'2.0','id':item['id'],'error':{'code':-32601,'message':'Client tools disabled'}})
                    raise NativeError('native_tool_request_rejected')
                # Passive notifications carry no authority to run tools or change policy.
                if item['method'] in ('session/update','_x.ai/session_notification'):
                    update=item.get('params',{}).get('update',{})
                    if update.get('sessionUpdate') in ('tool_call','tool_call_update','retry_state'):
                        raise NativeError('native_tool_observed')
                    self.notifications.append(item)
                    need(len(self.notifications)<=4096,'notification_limit')
                continue
            internal_id=internal_reload_ack(item)
            if internal_id is not None:
                counts=getattr(self,'internal_ack_counts',{})
                need(sum(counts.values())<8,'native_internal_reload_limit')
                counts[internal_id]=counts.get(internal_id,0)+1;self.internal_ack_counts=counts
                continue
            if type(item.get('id'))is not type(ident) or item.get('id')!=ident:
                failure=NativeError('unexpected_response');failure.frame_envelope=frame_envelope(item,ident)
                if method=='session/prompt':
                    prompt_id=params.get('_meta',{}).get('promptId')
                    failure.frame_envelope['prompt_id_correlation']=(
                        'not_provided'if type(prompt_id)is not str else
                        'matches'if type(item.get('id'))is str and item['id']==prompt_id else'different')
                raise failure
            if 'error' in item:
                code=item.get('error',{}).get('code')
                raise NativeError('native_rpc_'+str(code) if type(code)is int else 'native_rpc_failed')
            need('result'in item,'native_result_missing');return item['result']
    def close(self):
        if self.closed:return
        try:os.killpg(self.process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        self.process.wait(timeout=10)
        self.process.stdin.close();self.process.stdout.close();self.closed=True

def unwrap(value):
    need(isinstance(value,dict),'native_object_required')
    # Official ExtMethodResult<T> embeds result/error inside JSON-RPC result.
    if 'result'in value or 'error'in value:
        need(value.get('error')is None and isinstance(value.get('result'),dict),'native_extension_failed')
        return value['result']
    need('success'not in value and 'data'not in value,'native_extension_schema')
    return value
