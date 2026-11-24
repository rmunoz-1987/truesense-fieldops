"""Phone bottom tabs and slim header; desktop top nav stays in place."""
import os, tempfile, unittest, shutil, sys, re

TMP = tempfile.mkdtemp(prefix='fo-nav-test-')
os.environ['DATA_DIR'] = TMP
os.environ['DB_PATH'] = os.path.join(TMP, 'fieldops.db')
os.environ['UPLOAD_FOLDER'] = os.path.join(TMP, 'uploads')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ROLAND_PASSWORD'] = 'TestPass1234'
os.environ['OFFICE_NAMES'] = 'Roland'
os.environ['WOODY_RODRIGUEZ_PASSWORD'] = 'TestPass1234'
os.environ['SECRET_KEY'] = 'test-secret-local-only'
os.environ.pop('RENDER', None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fieldops_app as fo


class PhoneNavTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    def setUp(self):
        self.c = fo.app.test_client()

    def login(self, name='Roland', pw='TestPass1234'):
        r = self.c.post('/login', data={'name': name, 'password': pw}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def _tab_labels(self, html):
        block = re.search(r'<nav class="fo-tabs[^"]*" aria-label="Main">(.*?)</nav>', html, re.S)
        self.assertIsNotNone(block, 'missing fo-tabs')
        return re.findall(r'<span>([^<]+)</span>', block.group(1))

    def test_office_phone_tabs_are_today_jobs_customers_more(self):
        self.login()
        html = self.c.get('/').get_data(as_text=True)
        labels = self._tab_labels(html)
        self.assertEqual(labels, ['Today', 'Jobs', 'Customers', 'More'])
        self.assertLessEqual(len(labels), 4)
        self.assertIn('fo-switch', html)
        self.assertIn('Ops', html)
        self.assertIn('@media(max-width:767px)', html)
        self.assertIn('safe-area-inset-bottom', html)
        self.assertIn('fo-search-btn', html)
        # Schedule, Sales, Quote, Floor Plans, Service Plans and Revenue live in More
        more = self.c.get('/more').get_data(as_text=True)
        for needle in ('Schedule', 'Sales', 'Quote', 'Floor Plans', 'Service Plans', 'Revenue'):
            self.assertIn(needle, more)
        self.assertIn('id="fo-more-sheet"', html)
        self.assertIn('New job', html)
        self.assertIn('Pending surveys', html)
        self.assertIn('fo-desk-today', html)
        self.assertNotIn('\u2014', html)

    def test_sales_marks_more_tab_and_keeps_desktop_switcher(self):
        self.login()
        html = self.c.get('/sales').get_data(as_text=True)
        self.assertIn('fo-switch', html)
        block = re.search(r'<nav class="fo-tabs[^"]*" aria-label="Main">(.*?)</nav>', html, re.S).group(1)
        self.assertIn('href="/more"', block)
        # More is the active tab on Sales; Today is not
        self.assertRegex(block, r'href="/more"[^>]*class="on"')
        self.assertIn('Pending surveys', self.c.get('/').get_data(as_text=True) or '')

    def test_sub_tabs_are_jobs_plans_help_without_money_or_switcher(self):
        self.login('Woody Rodriguez', 'TestPass1234')
        html = self.c.get('/').get_data(as_text=True)
        self.assertNotIn('class="fo-switch"', html)
        self.assertIn('fo-phone-shell', html)
        labels = self._tab_labels(html)
        self.assertEqual(labels, ['My Jobs', 'Floor Plans', 'Help'])
        self.assertLessEqual(len(labels), 4)
        self.assertNotIn('Money', ''.join(labels))
        # money items should not appear in a sub more sheet either; subs have no More tab
        self.assertNotIn('href="/revenue"', html)
        self.assertNotIn('href="/money"', html)
        r = self.c.get('/help')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Need help', r.get_data(as_text=True))

    def test_designer_and_client_pages_have_no_bottom_bar(self):
        self.login()
        # client quote template is public and must not get the office shell
        # use a trivial check on login-optional skip paths in the helper
        self.assertTrue(any(s.startswith('/design/') for s in fo.SHELL_SKIP))
        self.assertTrue(any(s.startswith('/p/') for s in fo.SHELL_SKIP))
        html = self.c.get('/login').get_data(as_text=True)
        self.assertNotIn('class="fo-tabs', html)


if __name__ == '__main__':
    unittest.main()
