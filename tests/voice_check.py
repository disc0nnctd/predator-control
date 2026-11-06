import base64,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server as S
class Voice(unittest.TestCase):
 def setUp(self):
  self.old=S.remote;self.oldlog=S.log;S.log=lambda *args:None;self.scripts=[];self.running=False;self.enabled=False
  def remote(cmd,timeout=25):
   script=base64.b64decode(cmd.split('-EncodedCommand ')[1]).decode('utf-16-le');self.scripts.append(script)
   if 'Stop-ScheduledTask' in script:self.running=self.enabled=False
   if 'Start-ScheduledTask' in script:self.running=self.enabled=True
   return json.dumps({'running':self.running,'enabled':self.enabled,'state':'Running' if self.running else 'Disabled'})
  S.remote=remote;S.voice_cache.update(t=0,v=None)
 def tearDown(self):S.remote=self.old;S.log=self.oldlog
 def test_controls(self):
  self.assertFalse(S.voice_status(True)['running'])
  self.assertTrue(S.voice_control({'action':'start','confirm':True})['running']);self.assertIn('Enable-ScheduledTask',self.scripts[-1])
  self.assertTrue(S.voice_control({'action':'restart','confirm':True})['running']);self.assertLess(self.scripts[-1].index('Stop-ScheduledTask'),self.scripts[-1].index('Start-ScheduledTask'))
  result=S.voice_control({'action':'stop','confirm':True});self.assertFalse(result['running']);self.assertFalse(result['enabled']);self.assertIn('/T /F',self.scripts[-1])
 def test_reject_unconfirmed_and_arbitrary_commands(self):
  for body in [{'action':'start'},{'action':'start;calc','confirm':True}]:
   with self.assertRaises(S.Refuse):S.voice_control(body)
  self.assertEqual(self.scripts,[])
if __name__=='__main__':unittest.main(verbosity=2)
