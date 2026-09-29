import sys
import unittest
from pathlib import Path
from datetime import datetime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from calendar_tags import calendar_tags

class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'holiday':{'holiday_days':['2026-09-25','2026-09-26','2026-09-27']+['2026-10-%02d'%d for d in range(1,8)],'makeup_workdays':['2026-09-20'],'names':{'2026-09-25':'中秋','2026-10-01':'国庆'}}}
    def tag(self,s):return calendar_tags(datetime.fromisoformat(s),self.cfg)
    def test_scenes(self):
        self.assertEqual(self.tag('2026-09-20')['calendar_type'],'makeup_workday')
        self.assertEqual(self.tag('2026-09-28')['holiday_phase'],'between')
        self.assertEqual(self.tag('2026-09-30')['holiday_phase'],'eve')
        self.assertEqual(self.tag('2026-10-01')['scene'],'国庆 · 首日')
        self.assertEqual(self.tag('2026-10-06')['holiday_phase'],'return')
        self.assertNotEqual(self.tag('2026-09-25')['scene'],self.tag('2026-10-01')['scene'])
    def test_timezone(self):
        self.assertEqual(self.tag('2026-09-30T16:30:00+00:00')['scene'],'国庆 · 首日')

class LifecycleTests(unittest.TestCase):
    def test_cutoff(self):
        from lifecycle import project_active
        self.assertTrue(project_active(datetime.fromisoformat('2026-10-07T23:59:59+08:00')))
        self.assertFalse(project_active(datetime.fromisoformat('2026-10-08T00:00:00+08:00')))
        self.assertFalse(project_active(datetime.fromisoformat('2026-10-08T12:00:00+08:00')))
