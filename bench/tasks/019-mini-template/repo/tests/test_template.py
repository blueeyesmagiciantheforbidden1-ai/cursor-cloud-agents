import unittest
from minitemplate import render


class TestTemplate(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(render('Hi {{name}}', {'name': 'Ada'}), 'Hi Ada')

    def test_upper(self):
        self.assertEqual(render('{{name|upper}}', {'name': 'Ada'}), 'ADA')


if __name__ == "__main__":
    unittest.main()
