#
# Copyright 2018 The Cobalt Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Raspi implementation of Starboard launcher abstraction."""

import functools
import logging
import os
import re
import signal
import six
import sys
import threading
import time
import contextlib

import pexpect
from starboard.tools import abstract_launcher
from starboard.shared import retry

IS_MODULAR_BUILD = os.getenv('MODULAR_BUILD', '0') == '1'


class TargetPathError(ValueError):
  pass


# pylint: disable=unused-argument
def _sigint_or_sigterm_handler(signum, frame):
  """Clean up and exit with status |signum|.

  Args:
    signum: Signal number that triggered this callback.  Passed in when the
      signal handler is called by python runtime.
    frame: Current stack frame.  Passed in when the signal handler is called by
      python runtime.
  """
  sys.exit(signum)


# First call returns True, otherwise return false.
def first_run():
  v = globals()
  if 'first_run' not in v:
    v['first_run'] = False
    return True
  return False


class Launcher(abstract_launcher.AbstractLauncher):
  """Class for launching Cobalt/tools on Raspi."""

  _STARTUP_TIMEOUT_SECONDS = 1800

  _RASPI_USERNAME = 'pi'
  _RASPI_PASSWORD = 'raspberry'
  _SSH_LOGIN_SIGNAL = 'cobalt-launcher-login-success'
  _SSH_SLEEP_SIGNAL = 'cobalt-launcher-done-sleeping'
  _RASPI_PROMPT = 'pi@raspberrypi:'

  # pexpect times out each second to allow Kill to quickly stop a test run
  _PEXPECT_TIMEOUT = 1

  # SSH shell command retries
  _PEXPECT_SPAWN_RETRIES = 20

  # pexpect.sendline retries
  _PEXPECT_SENDLINE_RETRIES = 3

  # Old process kill retries
  _KILL_RETRIES = 3

  _PEXPECT_SHUTDOWN_SLEEP_TIME = 3
  # Time to wait after processes were killed
  _PROCESS_KILL_SLEEP_TIME = 10

  # Retrys for getting a clean prompt
  _PROMPT_WAIT_MAX_RETRIES = 5
  # Wait up to 10 seconds for the password prompt from the raspi
  _PEXPECT_PASSWORD_TIMEOUT_MAX_RETRIES = 10
  # Wait up to 600 seconds for new output from the raspi
  _PEXPECT_READLINE_TIMEOUT_MAX_RETRIES = 600
  # Delay between subsequent SSH commands
  _INTER_COMMAND_DELAY_SECONDS = 1.5

  # This is used to strip ansi color codes from pexpect output.
  _PEXPECT_SANITIZE_LINE_RE = re.compile(r'\x1b[^m]*m')

  # Exceptions to retry
  _RETRY_EXCEPTIONS = (pexpect.TIMEOUT, pexpect.ExceptionPexpect,
                       pexpect.exceptions.EOF, OSError)

  def __init__(self, platform, target_name, config, device_id, **kwargs):
    # pylint: disable=super-with-arguments
    super().__init__(platform, target_name, config, device_id, **kwargs)
    env = os.environ.copy()
    env.update(self.env_variables)
    self.full_env = env

    if not self.device_id:
      self.device_id = self.full_env.get('RASPI_ADDR')
      if not self.device_id:
        raise ValueError(
            'Unable to determine target, please pass it in, or set RASPI_ADDR '
            'environment variable.')

    self.startup_timeout_seconds = Launcher._STARTUP_TIMEOUT_SECONDS

    self.pexpect_process = None
    self._InitPexpectCommands()

    self.run_inactive = threading.Event()
    self.run_inactive.set()

    self.shutdown_initiated = threading.Event()

    self.log_targets = kwargs.get('log_targets', True)

    signal.signal(signal.SIGINT, functools.partial(_sigint_or_sigterm_handler))
    signal.signal(signal.SIGTERM, functools.partial(_sigint_or_sigterm_handler))

    self.last_run_pexpect_cmd = ''

  def _GetAndCheckTestFile(self, target_name):
    # TODO(b/218889313): This should reference the bin/ subdir when that's
    # used.
    test_dir = os.path.join(self.out_directory, 'install', target_name)
    test_file = target_name
    test_path = os.path.join(test_dir, test_file)

    if not os.path.isfile(test_path):
      raise TargetPathError(f'TargetPath ({test_path}) must be a file.')
    return test_file

  def _GetAndCheckTestFileWithFallback(self):
    try:
      return self._GetAndCheckTestFile(self.target_name + '_loader')
    except TargetPathError as e:
      if IS_MODULAR_BUILD:
        raise e
      return self._GetAndCheckTestFile(self.target_name)

  def _InitPexpectCommands(self):
    """Initializes all of the pexpect commands needed for running the test."""

    # Ensure no trailing slashes
    self.out_directory = self.out_directory.rstrip('/')

    test_file = self._GetAndCheckTestFileWithFallback()

    raspi_user_hostname = Launcher._RASPI_USERNAME + '@' + self.device_id

    # Use the basename of the out directory as a common directory on the device
    # so content can be reused for several targets w/o re-syncing for each one.
    raspi_test_dir = os.path.basename(self.out_directory)
    raspi_test_path = os.path.join(raspi_test_dir, test_file, test_file)

    # rsync command setup
    options = '-avzLh'
    source = os.path.join(self.out_directory, 'install') + '/'
    destination = f'{raspi_user_hostname}:~/{raspi_test_dir}/'
    self.rsync_command = 'rsync ' + options + ' ' + source + ' ' + destination

    # ssh command setup
    self.ssh_command = 'ssh -t ' + raspi_user_hostname + ' TERM=dumb bash -l'

    # escape command line metacharacters in the flags
    flags = ' '.join(self.target_command_line_params)
    meta_chars = '()[]{}%!^"<>&|'
    meta_re = re.compile('(' + '|'.join(
        re.escape(char) for char in list(meta_chars)) + ')')
    escaped_flags = re.subn(meta_re, r'\\\1', flags)[0]

    # test output tags
    self.test_complete_tag = f'TEST-{time.time()}'
    self.test_success_tag = 'succeeded'
    self.test_failure_tag = 'failed'

    # test command setup
    test_base_command = raspi_test_path + ' ' + escaped_flags
    test_success_output = (f' && echo {self.test_complete_tag} '
                           f'{self.test_success_tag}')
    test_failure_output = (f' || echo {self.test_complete_tag} '
                           f'{self.test_failure_tag}')
    self.test_command = (f'{test_base_command} {test_success_output} '
                         f'{test_failure_output}')

  # pylint: disable=no-method-argument
  def _CommandBackoff():
    time.sleep(Launcher._INTER_COMMAND_DELAY_SECONDS)

  def _ShutdownBackoff(self):
    Launcher._CommandBackoff()
    return self.shutdown_initiated.is_set()

  @retry.retry(
      exceptions=_RETRY_EXCEPTIONS,
      retries=_PEXPECT_SPAWN_RETRIES,
      backoff=_CommandBackoff)
  def _PexpectSpawnAndConnect(self, command):
    """Spawns a process with pexpect and connect to the raspi.

    Args:
       command: The command to use when spawning the pexpect process.
    """

    logging.info('executing: %s', command)
    kwargs = {} if six.PY2 else {'encoding': 'utf-8'}
    self.pexpect_process = pexpect.spawn(
        command, timeout=Launcher._PEXPECT_TIMEOUT, **kwargs)
    # Let pexpect output directly to our output stream
    self.pexpect_process.logfile_read = self.output_file
    expected_prompts = [
        r'.*Are\syou\ssure.*',  # Fingerprint verification
        r'.* password:',  # Password prompt
        '.*[a-zA-Z]+.*',  # Any other text input
    ]

    # pylint: disable=unnecessary-lambda
    @retry.retry(
        exceptions=Launcher._RETRY_EXCEPTIONS,
        retries=Launcher._PEXPECT_PASSWORD_TIMEOUT_MAX_RETRIES,
        backoff=lambda: self._ShutdownBackoff(),
        wrap_exceptions=False)
    def _inner():
      i = self.pexpect_process.expect(expected_prompts)
      if i == 0:
        self._PexpectSendLine('yes')
      elif i == 1:
        self._PexpectSendLine(Launcher._RASPI_PASSWORD)
      else:
        # If any other input comes in, maybe we've logged in with rsa key or
        # raspi does not have password. Check if we've logged in by echoing
        # a special sentence and expect it back.
        self._PexpectSendLine('echo ' + Launcher._SSH_LOGIN_SIGNAL)
        i = self.pexpect_process.expect([Launcher._SSH_LOGIN_SIGNAL])

    _inner()

  @retry.retry(
      exceptions=_RETRY_EXCEPTIONS,
      retries=_PEXPECT_SENDLINE_RETRIES,
      wrap_exceptions=False)
  def _PexpectSendLine(self, cmd):
    """Send lines to Pexpect and record the last command for logging purposes"""
    logging.info('sending >> : %s ', cmd)
    self.last_run_pexpect_cmd = cmd
    self.pexpect_process.sendline(cmd)

  def _PexpectReadLines(self):
    """Reads all lines from the pexpect process."""
    while True:
      # pylint: disable=unnecessary-lambda
      line = retry.with_retry(
          self.pexpect_process.readline,
          exceptions=Launcher._RETRY_EXCEPTIONS,
          retries=Launcher._PEXPECT_READLINE_TIMEOUT_MAX_RETRIES,
          backoff=lambda: self.shutdown_initiated.is_set(),
          wrap_exceptions=False)
      # Sanitize the line to remove ansi color codes.
      line = Launcher._PEXPECT_SANITIZE_LINE_RE.sub('', line)
      self.output_file.flush()
      if not line:
        return
      # Check for the test complete tag. It will be followed by either a
      # success or failure tag.
      if line.startswith(self.test_complete_tag):
        if line.find(self.test_success_tag) != -1:
          self.return_value = 0
        return

  def _Sleep(self, val):
    self._PexpectSendLine(f'sleep {val};echo {Launcher._SSH_SLEEP_SIGNAL}')
    self.pexpect_process.expect([Launcher._SSH_SLEEP_SIGNAL])

  def _CleanupPexpectProcess(self):
    """Closes current pexpect process."""

    if self.pexpect_process is not None and self.pexpect_process.isalive():
      # Check if kernel logged OOM kill or any other system failure message
      if self.return_value:
        logging.info('Sending dmesg')
        with contextlib.suppress(Launcher._RETRY_EXCEPTIONS):
          self._PexpectSendLine('dmesg -P --color=never | tail -n 100')
        time.sleep(self._PEXPECT_SHUTDOWN_SLEEP_TIME)
        with contextlib.suppress(Launcher._RETRY_EXCEPTIONS):
          self.pexpect_process.readlines()
        logging.info('Done sending dmesg')

      # Send ctrl-c to the raspi and close the process.
      with contextlib.suppress(Launcher._RETRY_EXCEPTIONS):
        self._PexpectSendLine(chr(3))
      time.sleep(self._PEXPECT_TIMEOUT)  # Allow time for normal shutdown
      with contextlib.suppress(Launcher._RETRY_EXCEPTIONS):
        self.pexpect_process.close()

  def _WaitForPrompt(self):
    """Sends empty commands, until a bash prompt is returned"""

    def backoff():
      self._PexpectSendLine('echo ' + Launcher._SSH_SLEEP_SIGNAL)
      return self._ShutdownBackoff()

    retry.with_retry(
        lambda: self.pexpect_process.expect(self._RASPI_PROMPT),
        exceptions=Launcher._RETRY_EXCEPTIONS,
        retries=Launcher._PROMPT_WAIT_MAX_RETRIES,
        backoff=backoff,
        wrap_exceptions=False)

  @retry.retry(
      exceptions=_RETRY_EXCEPTIONS,
      retries=_KILL_RETRIES,
      backoff=_CommandBackoff)
  def _KillExistingCobaltProcesses(self):
    """If there are leftover Cobalt processes, kill them.

    It is possible that a previous process did not exit cleanly.
    Zombie Cobalt instances can block the WebDriver port or
    cause other problems.
    """
    logging.info('Killing existing processes')
    self._PexpectSendLine(
        'pkill -9 -ef "(cobalt)|(crashpad_handler)|(elf_loader)"')
    self._WaitForPrompt()
    # Print the return code of pkill. 0 if a process was halted
    self._PexpectSendLine('echo PROCKILL:${?}')
    i = self.pexpect_process.expect([r'PROCKILL:0', r'PROCKILL:(\d+)'])
    if i == 0:
      logging.warning('Forced to pkill existing instance(s) of cobalt. '
                      'Pausing to ensure no further operations are run '
                      'before processes shut down.')
      time.sleep(Launcher._PROCESS_KILL_SLEEP_TIME)
    logging.info('Done killing existing processes')

  def Run(self):
    """Runs launcher's executable on the target raspi.

    Returns:
       Whether or not the run finished successfully.
    """

    if self.log_targets:
      logging.info('-' * 32)
      logging.info('Starting to run target: %s', self.target_name)
      logging.info('=' * 32)

    self.return_value = 1

    try:
      # Notify other threads that the run is now active
      self.run_inactive.clear()

      # rsync the test files to the raspi
      if not self.shutdown_initiated.is_set():
        self._PexpectSpawnAndConnect(self.rsync_command)
      if not self.shutdown_initiated.is_set():
        self._PexpectReadLines()

      # ssh into the raspi and run the test
      if not self.shutdown_initiated.is_set():
        self._PexpectSpawnAndConnect(self.ssh_command)
        self._Sleep(self._INTER_COMMAND_DELAY_SECONDS)
      # Execute debugging commands on the first run
      first_run_commands = []
      if self.test_result_xml_path:
        first_run_commands.append(f'touch {self.test_result_xml_path}')
      first_run_commands.extend(['free -mh', 'ps -ux', 'df -h'])
      if first_run():
        for cmd in first_run_commands:
          if not self.shutdown_initiated.is_set():
            self._PexpectSendLine(cmd)

            def _readline():
              line = self.pexpect_process.readline()
              self.output_file.write(line)

            retry.with_retry(
                _readline,
                exceptions=Launcher._RETRY_EXCEPTIONS,
                retries=Launcher._PROMPT_WAIT_MAX_RETRIES)
        self._WaitForPrompt()
        self.output_file.flush()
        self._Sleep(self._INTER_COMMAND_DELAY_SECONDS)
        self._KillExistingCobaltProcesses()
        self._Sleep(self._INTER_COMMAND_DELAY_SECONDS)

      if not self.shutdown_initiated.is_set():
        self._PexpectSendLine(self.test_command)
        self._PexpectReadLines()

    except retry.RetriesExceeded:
      logging.exception('Command retry exceeded (cmd: %s)',
                        self.last_run_pexpect_cmd)
    except pexpect.EOF:
      logging.exception('pexpect encountered EOF while reading line. (cmd: %s)',
                        self.last_run_pexpect_cmd)
    except pexpect.TIMEOUT:
      logging.exception('pexpect timed out while reading line. (cmd: %s)',
                        self.last_run_pexpect_cmd)
    except Exception:  # pylint: disable=broad-except
      logging.exception('Error occurred while running test. (cmd: %s)',
                        self.last_run_pexpect_cmd)
    finally:
      self._CleanupPexpectProcess()

      # Notify other threads that the run is no longer active
      self.run_inactive.set()

    if self.log_targets:
      logging.info('-' * 32)
      logging.info('Finished running target: %s', self.target_name)
      logging.info('=' * 32)

    return self.return_value

  def Kill(self):
    """Stops the run so that the launcher can be killed."""

    sys.stderr.write('\n***Killing Launcher***\n')
    if self.run_inactive.is_set():
      return
    # Initiate the shutdown. This causes the run to abort within one second.
    self.shutdown_initiated.set()
    # Wait up to three seconds for the run to be set to inactive.
    self.run_inactive.wait(Launcher._PEXPECT_SHUTDOWN_SLEEP_TIME)

  def GetDeviceIp(self):
    """Gets the device IP."""
    return self.device_id

  def GetDeviceOutputPath(self):
    """Writable path where test targets can output files"""
    return '/tmp'
