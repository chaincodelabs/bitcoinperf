from setuptools import setup
import os

here = os.path.abspath(os.path.dirname(__file__))
namespace = {}

setup(
    name='bitcoinperf',
    version='0.0.1',
    description="Bitcoin Core performance benchmarking tools",
    author='jamesob',
    author_email='jamesob@chaincode.com',
    py_modules=['runner'],
    include_package_data=True,
    zip_safe=False,
    install_requires=open(os.path.join(
        here, 'runner', 'requirements.txt')).readlines(),
    entry_points={
        'console_scripts': [
            'bitcoinperf = runner.main:main',
            'bitcoinperf-hwinfo = runner.hwinfo:main',
            'bitcoinperf-perf-check = runner.perftools:perf_check_cli',
        ],
    },
)
