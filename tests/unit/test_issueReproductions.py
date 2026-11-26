# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2026 NV Access Limited
# This file may be used under the terms of the GNU General Public License, version 2 or later, as modified by the NVDA license.
# For full terms and any additional permissions, see the NVDA license file: https://github.com/nvaccess/nvda/blob/master/copying.txt

"""Reproductions of the original issues, exercised through the real code paths.

These tests are expected to FAIL on unmodified master (reproducing each issue)
and PASS once the corresponding fix is applied.
They are only used to verify the fixes in CI, and are not part of the pull requests.
"""

import logging
import os
import pickle
import tempfile
import unittest
from unittest import mock

import audio
import buildVersion
import config
import globalVars
from logHandler import log

import NVDAHelper

with mock.patch.object(buildVersion, "updateVersionType", "snapshot:alpha"):
	import updateCheck


# --- Sound split on secure screens (#19872) ---


class _FakeChannelVolume:
	def __init__(self, left: float, right: float):
		self.volumes = [left, right]

	def GetChannelCount(self) -> int:
		return 2

	def SetChannelVolume(self, channel: int, volume: float, context) -> None:
		self.volumes[channel] = volume


class _FakeAudioSession:
	"""Stands in for a pycaw AudioSession of another application."""

	def __init__(self, pid: int, left: float = 1.0, right: float = 1.0):
		self.ProcessId = pid
		self._channelVolume = _FakeChannelVolume(left, right)

	def channelAudioVolume(self) -> _FakeChannelVolume:
		return self._channelVolume

	def register_notification(self, callback) -> None:
		pass

	def unregister_notification(self) -> None:
		pass

	@property
	def volumes(self) -> list[float]:
		return self._channelVolume.volumes


@unittest.skipUnless(audio.SOUND_SPLIT_AVAILABLE, "Sound Split dependencies are unavailable")
class Repro19872_SoundSplitOnSecureScreen(unittest.TestCase):
	"""Uses the real audio.soundSplit module; only the Windows audio session APIs are faked."""

	APP_PID = 4242

	def setUp(self):
		soundSplit = audio.soundSplit
		self.soundSplit = soundSplit
		originalState = config.conf["audio"]["soundSplitState"]
		self.addCleanup(config.conf["audio"].__setitem__, "soundSplitState", originalState)
		for patcher in (
			mock.patch.object(soundSplit, "_audioSessionManager", None),
			mock.patch.object(soundSplit, "_activeCallback", None),
			mock.patch.dict(soundSplit._applicationExitCallbacks, clear=True),
			mock.patch.object(
				soundSplit.AudioUtilities,
				"GetAudioSessionManager",
				return_value=mock.MagicMock(),
			),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def patchSecureScreen(self, isSecure: bool) -> None:
		for patcher in (
			mock.patch("utils.security.isRunningOnSecureDesktop", return_value=isSecure),
			# On fixed code, audio binds the function at import time.
			mock.patch.object(audio, "isRunningOnSecureDesktop", return_value=isSecure, create=True),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def patchSessions(self, *sessions: _FakeAudioSession) -> None:
		patcher = mock.patch.object(
			self.soundSplit.AudioUtilities,
			"GetAllSessions",
			return_value=list(sessions),
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_activatingSoundSplitOnSecureScreenLeavesApplicationVolumesAlone(self):
		"""Issue steps: on the secure screen, activate sound split.

		Before the fix: all application audio moves to one channel, and stays there after leaving the secure screen.
		"""
		self.patchSecureScreen(True)
		appSession = _FakeAudioSession(self.APP_PID)
		self.patchSessions(appSession)
		# Before the fix, the secure screen copy of NVDA initialized sound split at startup.
		self.soundSplit._audioSessionManager = mock.MagicMock()

		audio._setSoundSplitState(audio.SoundSplitState.NVDA_LEFT_APPS_RIGHT)

		self.assertEqual([1.0, 1.0], appSession.volumes)

	def test_cycleCommandOnSecureScreenLeavesApplicationVolumesAlone(self):
		"""Issue steps using NVDA+alt+s (cycle sound split) on the secure screen."""
		self.patchSecureScreen(True)
		appSession = _FakeAudioSession(self.APP_PID)
		self.patchSessions(appSession)
		config.conf["audio"]["soundSplitState"] = audio.SoundSplitState.OFF.value
		# Before the fix, the secure screen copy of NVDA initialized sound split at startup.
		self.soundSplit._audioSessionManager = mock.MagicMock()

		with mock.patch("ui.message"):
			audio._toggleSoundSplitState()

		self.assertEqual([1.0, 1.0], appSession.volumes)

	def test_secureScreenCopyWithSoundSplitConfiguredDoesNotTouchUserSoundSplit(self):
		"""Settings with sound split enabled were copied to the secure screen configuration.

		The user's own NVDA has applications on the left (1.0, 0.0).
		Before the fix, the secure screen copy of NVDA moves them to the right when it starts,
		and resets them to both channels when it exits.
		"""
		self.patchSecureScreen(True)
		appSession = _FakeAudioSession(self.APP_PID, left=1.0, right=0.0)
		self.patchSessions(appSession)
		config.conf["audio"]["soundSplitState"] = audio.SoundSplitState.NVDA_LEFT_APPS_RIGHT.value

		audio.initialize()
		self.assertEqual([1.0, 0.0], appSession.volumes, "changed when the secure screen copy started")
		audio.terminate()
		# soundSplit.terminate is also registered with atexit.
		self.soundSplit.terminate()
		self.assertEqual([1.0, 0.0], appSession.volumes, "changed when the secure screen copy exited")

	def test_control_soundSplitStillWorksOnUserDesktop(self):
		"""Control: on the normal desktop, sound split must keep working."""
		self.patchSecureScreen(False)
		appSession = _FakeAudioSession(self.APP_PID)
		nvdaSession = _FakeAudioSession(globalVars.appPid)
		self.patchSessions(appSession, nvdaSession)
		self.soundSplit._audioSessionManager = mock.MagicMock()

		audio._setSoundSplitState(audio.SoundSplitState.NVDA_LEFT_APPS_RIGHT)

		self.assertEqual([0.0, 1.0], appSession.volumes)
		self.assertEqual([1.0, 0.0], nvdaSession.volumes)


# --- log.exception interpolation when the remote loader fails to start ---


class ReproRemoteLoaderLog_LogExceptionInterpolation(unittest.TestCase):
	def test_failureToStartRemoteLoaderLogsTheLoaderDirectory(self):
		"""Before the fix: the log says "Unable to start remote loader from %s" and never names the directory."""
		records: list[logging.LogRecord] = []
		handler = logging.Handler()
		handler.emit = records.append
		log.addHandler(handler)
		self.addCleanup(log.removeHandler, handler)
		originalLevel = log.level
		log.setLevel(logging.DEBUG)
		self.addCleanup(log.setLevel, originalLevel)
		loaderDir = "C:\\fake NVDA\\lib64"

		with mock.patch.object(NVDAHelper, "_RemoteLoader", side_effect=OSError("loader failed")):
			self.assertIsNone(NVDAHelper._tryStartRemoteLoader(loaderDir))

		messages = [record.getMessage() for record in records]
		self.assertIn(f"Unable to start remote loader from {loaderDir}", messages)
		failureRecord = next(r for r in records if r.getMessage().startswith("Unable to start remote loader"))
		self.assertEqual(logging.ERROR, failureRecord.levelno)
		# The real OSError must be the logged exception.
		self.assertIsInstance(failureRecord.exc_info[1], OSError)


# --- Update check state loaded with pickle (#19560) ---


class _MaliciousPayload:
	"""When unpickled, creates a directory: a harmless stand-in for arbitrary code execution."""

	def __init__(self, markerPath: str):
		self.markerPath = markerPath

	def __reduce__(self):
		return (os.mkdir, (self.markerPath,))


class Repro19560_UpdateCheckStatePickle(unittest.TestCase):
	def setUp(self):
		tempDir = tempfile.TemporaryDirectory()
		self.addCleanup(tempDir.cleanup)
		self.configDir = tempDir.name
		self.updatesDir = os.path.join(self.configDir, "updates")
		os.mkdir(self.updatesDir)
		self.markerPath = os.path.join(self.configDir, "codeExecutedByUnpickling")
		self.picklePath = os.path.join(self.configDir, "updateCheckState.pickle")
		with open(self.picklePath, "wb") as pickleFile:
			pickle.dump(_MaliciousPayload(self.markerPath), pickleFile)
		for patcher in (
			mock.patch.object(globalVars.appArgs, "configPath", self.configDir),
			mock.patch.object(globalVars.appArgs, "launcher", False),
			mock.patch.object(globalVars.appArgs, "secure", False),
			mock.patch.object(updateCheck, "storeUpdatesDir", self.updatesDir),
			mock.patch.object(updateCheck, "AutoUpdateChecker"),
			mock.patch.object(updateCheck, "state", None),
			mock.patch.object(updateCheck, "autoChecker", None),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_startingNvdaWithTamperedStateFileDoesNotExecuteCode(self):
		"""Before the fix: updateCheck.initialize() unpickles the file, running the embedded code."""
		updateCheck.initialize()

		self.assertFalse(os.path.exists(self.markerPath), "code embedded in the state file was executed")
		self.assertIsInstance(updateCheck.state, dict)
		self.assertRegex(updateCheck.state["id"], r"^[0-9a-f]{32}$")

	def test_tamperedPickleFileIsRemoved(self):
		updateCheck.initialize()

		self.assertFalse(os.path.exists(self.picklePath))

	def test_postponedUpdateSurvivesRestart(self):
		"""Control for the new format: a postponed update is still offered after restarting NVDA."""
		os.remove(self.picklePath)
		updateCheck.initialize()
		# The update is downloaded and postponed after NVDA has started.
		pendingFile = os.path.join(self.updatesDir, "nvda_update.exe")
		with open(pendingFile, "wb"):
			pass
		updateCheck.state["pendingUpdateFile"] = pendingFile
		updateCheck.state["pendingUpdateVersion"] = "2099.1"
		updateCheck.state["pendingUpdateAPIVersion"] = (2099, 1, 0)
		updateCheck.state["pendingUpdateBackCompatToAPIVersion"] = (2099, 1, 0)
		updateCheck.saveState()

		# Restart.
		updateCheck.state = None
		updateCheck.initialize()

		self.assertEqual(
			(pendingFile, "2099.1", (2099, 1, 0), (2099, 1, 0)),
			updateCheck.getPendingUpdate(),
		)
		self.assertTrue(os.path.isfile(pendingFile))
