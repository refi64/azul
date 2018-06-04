import setuptools
import setuptools.command.install

import os.path
import subprocess

class PostInstallCommand(setuptools.command.install.install):
    def run(self):
        super(PostInstallCommand, self).run()

        print('Running glib-compile-schemas...')
        subprocess.run(['glib-compile-schemas',
                        os.path.join(self.prefix, 'share', 'glib-2.0', 'schemas')],
                       check=True)

with open('README.md') as fp:
    long_description = fp.read()

icon_files = []
for item in os.listdir('misc/icons'):
    if 'x' not in item:
        continue

    icon_files.append((f'share/icons/hicolor/{item}/apps',
                       [f'misc/icons/{item}/com.refi64.Azul.png']))

setuptools.setup(
    name='azul',
    version='0.1.0',
    author='Ryan Gonzalez',
    author_email='rymg19@gmail.com',
    description='A native, GTK+-powered Zulip desktop client',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/kirbyfan64/azul',
    py_modules=['azul'],
    entry_points={
        'console_scripts': ['azul = azul:main']
    },
    data_files=[
        ('share/applications', ['misc/com.refi64.Azul.desktop']),
        ('share/appdata', ['misc/com.refi64.Azul.appdata.xml']),
        ('share/glib-2.0/schemas', ['misc/com.refi64.Azul.gschema.xml']),
        *icon_files
    ],
    cmdclass={
        'install': PostInstallCommand,
    },
    classifiers=[
        'Environment :: X11 Applications :: GTK',
        'License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)',
        'Programming Language :: Python :: 3.6 :: Only',
        'Topic :: Communications :: Chat',
    ],
)
