# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2016 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Showing prompts above the statusbar."""

import os.path
import html
import collections

import sip
from PyQt5.QtCore import (pyqtSlot, pyqtSignal, Qt, QTimer, QDir, QModelIndex,
                          QItemSelectionModel)
from PyQt5.QtWidgets import (QWidget, QGridLayout, QVBoxLayout, QLineEdit,
                             QLabel, QWidgetItem, QFileSystemModel, QTreeView,
                             QSizePolicy)

from qutebrowser.config import style
from qutebrowser.utils import usertypes, log, utils, qtutils, objreg
from qutebrowser.keyinput import modeman
from qutebrowser.commands import cmdutils, cmdexc


AuthTuple = collections.namedtuple('AuthTuple', ['user', 'password'])


class Error(Exception):

    """Base class for errors in this module."""


class UnsupportedOperationError(Exception):

    """Raised when the prompt class doesn't support the requested operation."""


class PromptContainer(QWidget):

    """Container for prompts to be shown above the statusbar.

    The way in which multiple questions are handled deserves some explanation.

    If a question is blocking, we *need* to ask it immediately, and can't wait
    for previous questions to finish. We could theoretically ask a blocking
    question inside of another blocking one, so in ask_question we simply save
    the current prompt state on the stack, let the user answer the *most
    recent* question, and then restore the previous state.

    With a non-blocking question, things are a bit easier. We simply add it to
    self._queue if we're still busy handling another question, since it can be
    answered at any time.

    In either case, as soon as we finished handling a question, we call
    _pop_later() which schedules a _pop to ask the next question in _queue. We
    schedule it rather than doing it immediately because then the order of how
    things happen is clear, e.g. on_mode_left can't happen after we already set
    up the *new* question.

    Attributes:
        _shutting_down: Whether we're currently shutting down the prompter and
                        should ignore future questions to avoid segfaults.
        _loops: A list of local EventLoops to spin in when blocking.
        _queue: A deque of waiting questions.
        _prompt: The current prompt object if we're handling a question.
        _layout: The layout used to show prompts in.
        _win_id: The window ID this object is associated with.
    """

    STYLESHEET = """
        {% set prompt_radius = config.get('ui', 'prompt-radius') %}
        QWidget#Prompt {
            {% if config.get('ui', 'status-position') == 'top' %}
                border-bottom-left-radius: {{ prompt_radius }}px;
                border-bottom-right-radius: {{ prompt_radius }}px;
            {% else %}
                border-top-left-radius: {{ prompt_radius }}px;
                border-top-right-radius: {{ prompt_radius }}px;
            {% endif %}
        }

        QWidget {
            font: {{ font['prompts'] }};
            color: {{ color['prompts.fg'] }};
            background-color: {{ color['prompts.bg'] }};
        }
    """
    update_geometry = pyqtSignal()

    def __init__(self, win_id, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._prompt = None
        self._shutting_down = False
        self._loops = []
        self._queue = collections.deque()
        self._win_id = win_id

        self.setObjectName('Prompt')
        self.setAttribute(Qt.WA_StyledBackground, True)
        style.set_register_stylesheet(self)

    def __repr__(self):
        return utils.get_repr(self, loops=len(self._loops),
                              queue=len(self._queue), prompt=self._prompt)

    def _pop_later(self):
        """Helper to call self._pop as soon as everything else is done."""
        QTimer.singleShot(0, self._pop)

    def _pop(self):
        """Pop a question from the queue and ask it, if there are any."""
        log.prompt.debug("Popping from queue {}".format(self._queue))
        if self._queue:
            question = self._queue.popleft()
            if not sip.isdeleted(question):
                # the question could already be deleted, e.g. by a cancelled
                # download. See
                # https://github.com/The-Compiler/qutebrowser/issues/415
                self.ask_question(question, blocking=False)

    def _show_prompt(self, prompt):
        """SHow the given prompt object.

        Args:
            prompt: A Prompt object or None.

        Return: True if a prompt was shown, False otherwise.
        """
        # Before we set a new prompt, make sure the old one is what we expect
        # This will also work if self._prompt is None and verify nothing is
        # displayed.
        #
        # Note that we don't delete the old prompt here, as we might be in the
        # middle of saving/restoring an old prompt object.
        assert self._layout.count() in [0, 1], self._layout.count()
        item = self._layout.takeAt(0)
        if item is None:
            assert self._prompt is None, self._prompt
        else:
            if (not isinstance(item, QWidgetItem) or
                    item.widget() is not self._prompt):
                raise AssertionError("Expected {} to be in layout but got "
                                    "{}!".format(self._prompt, item))
            item.widget().hide()

        log.prompt.debug("Displaying prompt {}".format(prompt))
        self._prompt = prompt
        if prompt is None:
            self.hide()
            return False

        prompt.question.aborted.connect(
            lambda: modeman.maybe_leave(self._win_id, prompt.KEY_MODE,
                                        'aborted'))
        modeman.enter(self._win_id, prompt.KEY_MODE, 'question asked')
        self._prompt = prompt
        self.setSizePolicy(self._prompt.sizePolicy())
        self._layout.addWidget(self._prompt)
        self._prompt.show()
        self.show()
        self._prompt.setFocus()
        self.update_geometry.emit()
        return True

    def shutdown(self):
        """Cancel all blocking questions.

        Quits and removes all running event loops.

        Return:
            True if loops needed to be aborted,
            False otherwise.
        """
        self._shutting_down = True
        if self._loops:
            for loop in self._loops:
                loop.quit()
                loop.deleteLater()
            return True
        else:
            return False

    @cmdutils.register(instance='prompt-container', hide=True, scope='window',
                       modes=[usertypes.KeyMode.prompt,
                              usertypes.KeyMode.yesno])
    def prompt_accept(self, value=None):
        """Accept the current prompt.

        //

        This executes the next action depending on the question mode, e.g. asks
        for the password or leaves the mode.

        Args:
            value: If given, uses this value instead of the entered one.
                   For boolean prompts, "yes"/"no" are accepted as value.
        """
        try:
            done = self._prompt.accept(value)
        except Error as e:
            raise cmdexc.CommandError(str(e))
        if done:
            key_mode = self._prompt.KEY_MODE
            self._prompt.question.done()
            modeman.maybe_leave(self._win_id, key_mode, ':prompt-accept')

    @cmdutils.register(instance='prompt-container', hide=True, scope='window',
                       modes=[usertypes.KeyMode.yesno],
                       deprecated='Use :prompt-accept yes instead!')
    def prompt_yes(self):
        """Answer yes to a yes/no prompt."""
        self.prompt_accept('yes')

    @cmdutils.register(instance='prompt-container', hide=True, scope='window',
                       modes=[usertypes.KeyMode.yesno],
                       deprecated='Use :prompt-accept no instead!')
    def prompt_no(self):
        """Answer no to a yes/no prompt."""
        self.prompt_accept('no')

    @pyqtSlot(usertypes.KeyMode)
    def on_mode_left(self, mode):
        """Clear and reset input when the mode was left."""
        # FIXME when is this not the case?
        if (self._prompt is not None and
                mode == self._prompt.KEY_MODE):
            question = self._prompt.question
            self._show_prompt(None)
            # FIXME move this somewhere else?
            if question.answer is None and not question.is_aborted:
                question.cancel()

    @cmdutils.register(instance='prompt-container', hide=True, scope='window',
                       modes=[usertypes.KeyMode.prompt], maxsplit=0)
    def prompt_open_download(self, cmdline: str=None):
        """Immediately open a download.

        If no specific command is given, this will use the system's default
        application to open the file.

        Args:
            cmdline: The command which should be used to open the file. A `{}`
                     is expanded to the temporary file name. If no `{}` is
                     present, the filename is automatically appended to the
                     cmdline.
        """
        try:
            self._prompt.download_open(cmdline)
        except UnsupportedOperationError:
            pass

    @cmdutils.register(instance='prompt-container', hide=True, scope='window',
                       modes=[usertypes.KeyMode.prompt])
    @cmdutils.argument('which', choices=['next', 'prev'])
    def prompt_item_focus(self, which):
        """Shift the focus of the prompt file completion menu to another item.

        Args:
            which: 'next', 'prev'
        """
        try:
            self._prompt.item_focus(which)
        except UnsupportedOperationError:
            pass

    @pyqtSlot(usertypes.Question, bool)
    def ask_question(self, question, blocking):
        """Display a prompt for a given question.

        Args:
            question: The Question object to ask.
            blocking: If True, this function blocks and returns the result.

        Return:
            The answer of the user when blocking=True.
            None if blocking=False.
        """
        log.prompt.debug("Asking question {}, blocking {}, loops {}, queue "
                         "{}".format(question, blocking, self._loops,
                                     self._queue))

        if self._shutting_down:
            # If we're currently shutting down we have to ignore this question
            # to avoid segfaults - see
            # https://github.com/The-Compiler/qutebrowser/issues/95
            log.prompt.debug("Ignoring question because we're shutting down.")
            question.abort()
            return None

        if self._prompt is not None and not blocking:
            # We got an async question, but we're already busy with one, so we
            # just queue it up for later.
            log.prompt.debug("Adding {} to queue.".format(question))
            self._queue.append(question)
            return

        if blocking:
            # If we're blocking we save the old state on the stack, so we can
            # restore it after exec, if exec gets called multiple times.
            old_prompt = self._prompt

        classes = {
            usertypes.PromptMode.yesno: YesNoPrompt,
            usertypes.PromptMode.text: LineEditPrompt,
            usertypes.PromptMode.user_pwd: AuthenticationPrompt,
            usertypes.PromptMode.download: DownloadFilenamePrompt,
            usertypes.PromptMode.alert: AlertPrompt,
        }
        klass = classes[question.mode]
        self._show_prompt(klass(question, self._win_id))
        if blocking:
            loop = qtutils.EventLoop()
            self._loops.append(loop)
            loop.destroyed.connect(lambda: self._loops.remove(loop))
            question.completed.connect(loop.quit)
            question.completed.connect(loop.deleteLater)
            loop.exec_()
            # FIXME don't we end up connecting modeman signals twice here now?
            if not self._show_prompt(old_prompt):
                # Nothing left to restore, so we can go back to popping async
                # questions.
                if self._queue:
                    self._pop_later()
            return question.answer
        else:
            question.completed.connect(self._pop_later)


class LineEdit(QLineEdit):

    """A line edit used in prompts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QLineEdit {
                border: 1px solid grey;
                background-color: transparent;
            }
        """)
        self.setAttribute(Qt.WA_MacShowFocusRect, False)

    def keyPressEvent(self, e):
        """Override keyPressEvent to paste primary selection on Shift + Ins."""
        if e.key() == Qt.Key_Insert and e.modifiers() == Qt.ShiftModifier:
            try:
                text = utils.get_clipboard(selection=True)
            except utils.ClipboardError:  # pragma: no cover
                pass
            else:
                e.accept()
                self.insert(text)
                return
        super().keyPressEvent(e)

    def __repr__(self):
        return utils.get_repr(self)


class _BasePrompt(QWidget):

    """Base class for all prompts."""

    KEY_MODE = usertypes.KeyMode.prompt

    def __init__(self, question, win_id, parent=None):
        super().__init__(parent)
        self.question = question
        self._win_id = win_id
        self._vbox = QVBoxLayout(self)
        self._vbox.setSpacing(15)
        self._key_grid = None
        self._key_grid_item = None

    def __repr__(self):
        return utils.get_repr(self, question=self.question, constructor=True)

    def _init_title(self, question):
        assert question.title is not None, question
        title_label = QLabel('<b>{}</b>'.format(question.title), self)
        self._vbox.addWidget(title_label)
        if question.text is not None:
            text_label = QLabel(question.text)
            self._vbox.addWidget(text_label)

    def _init_key_label(self):
        # Remove old grid
        if self._key_grid is not None:
            self._vbox.removeItem(self._key_grid_item)

        self._key_grid = QGridLayout()
        self._key_grid.setVerticalSpacing(0)

        key_config = objreg.get('key-config')
        # The bindings are all in the 'prompt' mode, even for yesno prompts
        all_bindings = key_config.get_reverse_bindings_for('prompt')
        labels = []

        for cmd, text in self._allowed_commands():
            bindings = all_bindings.get(cmd, [])
            if bindings:
                binding = None
                preferred = ['<enter>', '<escape>']
                for pref in preferred:
                    if pref in bindings:
                        binding = pref
                if binding is None:
                    binding = bindings[0]
                key_label = QLabel('<b>{}</b>'.format(html.escape(binding)))
                text_label = QLabel(text)
                labels.append((key_label, text_label))

        for i, (key_label, text_label) in enumerate(labels):
            self._key_grid.addWidget(key_label, i, 0)
            self._key_grid.addWidget(text_label, i, 1)

        self._vbox.addLayout(self._key_grid)
        self._key_grid_item = self._vbox.itemAt(self._vbox.count() - 1)

    def accept(self, value=None):
        raise NotImplementedError

    def download_open(self, _cmdline):
        """Open the download directly if this is a download prompt."""
        raise UnsupportedOperationError

    def item_focus(self, _which):
        """Switch to next file item if this is a filename prompt.."""
        raise UnsupportedOperationError

    def _allowed_commands(self):
        """Get the commands we could run as response to this message."""
        raise NotImplementedError


class LineEditPrompt(_BasePrompt):

    """A prompt for a single text value."""

    def __init__(self, question, win_id, parent=None):
        super().__init__(question, win_id, parent)
        self._lineedit = LineEdit(self)
        self._init_title(question)
        self._vbox.addWidget(self._lineedit)
        if question.default:
            self._lineedit.setText(question.default)
        self.setFocusProxy(self._lineedit)
        self._init_key_label()

    def accept(self, value=None):
        text = value if value is not None else self._lineedit.text()
        self.question.answer = text
        return True

    def _allowed_commands(self):
        """Get the commands we could run as response to this message."""
        return [('prompt-accept', 'Accept'), ('leave-mode', 'Abort')]


class FilenamePrompt(_BasePrompt):

    """A prompt for a filename."""

    def __init__(self, question, win_id, parent=None):
        super().__init__(question, win_id, parent)
        self._init_title(question)
        self._init_fileview()
        self._set_fileview_root(question.default)

        self._lineedit = LineEdit(self)
        self._lineedit.textChanged.connect(self._set_fileview_root)
        self._vbox.addWidget(self._lineedit)

        if question.default:
            self._lineedit.setText(question.default)
        self.setFocusProxy(self._lineedit)
        self._init_key_label()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    @pyqtSlot(str)
    def _set_fileview_root(self, path):
        """Set the root path for the file display."""
        if not path.endswith('/') or path == '/':
            return
        path.rstrip('/')

        try:
            if os.path.isdir(path):
                path = path
            elif os.path.isdir(os.path.basename(path)):
                path = os.path.basename(path)
            else:
                path = None
        except OSError:
            path = None

        if path is None:
            return

        root = self._file_model.setRootPath(path)
        self._file_view.setRootIndex(root)

    @pyqtSlot(QModelIndex)
    def _insert_path(self, index, *, clicked=True):
        """Handle an element selection.

        Args:
            index: The QModelIndex of the selected element.
            clicked: Whether the element was clicked.
        """
        parts = []
        cur = index
        while cur.isValid():
            parts.append(cur.data())
            cur = cur.parent()
        path = os.path.normpath(os.path.join(*reversed(parts)))
        if clicked:
            path += os.sep
        log.prompt.debug('Clicked {!r} -> {}'.format(parts, path))
        self._lineedit.setText(path)
        self._lineedit.setFocus()
        if clicked:
            # Avoid having a ..-subtree highlighted
            self._file_view.setCurrentIndex(QModelIndex())

    def _init_fileview(self):
        self._file_view = QTreeView(self)
        self._file_model = QFileSystemModel(self)
        self._file_view.setModel(self._file_model)
        self._file_view.clicked.connect(self._insert_path)
        self._vbox.addWidget(self._file_view)
        # Only show name
        self._file_view.setHeaderHidden(True)
        for col in range(1, 4):
            self._file_view.setColumnHidden(col, True)

    def accept(self, value=None):
        text = value if value is not None else self._lineedit.text()
        self.question.answer = text
        return True

    def item_focus(self, which):
        # This duplicates some completion code, but I don't see a nicer way...
        assert which in ['prev', 'next'], which
        selmodel = self._file_view.selectionModel()

        first_index = self._file_model.index(0, 0)
        last_index = self._file_model.index(self._file_model.rowCount() - 1, 0)

        idx = selmodel.currentIndex()
        if not idx.isValid():
            # No item selected yet
            idx = last_index if which == 'prev' else first_index

        if which == 'prev':
            idx = self._file_view.indexAbove(idx)
        else:
            idx = self._file_view.indexBelow(idx)
        # wrap around if we arrived at beginning/end
        if not idx.isValid():
            idx = last_index if which == 'prev' else first_index

        selmodel.setCurrentIndex(
            idx, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        self._insert_path(idx, clicked=False)

    def _allowed_commands(self):
        """Get the commands we could run as response to this message."""
        return [('prompt-accept', 'Accept'), ('leave-mode', 'Abort')]


class DownloadFilenamePrompt(FilenamePrompt):

    """A prompt for a filename for downloads."""

    def __init__(self, question, win_id, parent=None):
        super().__init__(question, win_id, parent)
        self._file_model.setFilter(QDir.AllDirs | QDir.Drives | QDir.NoDot)

    def accept(self, value=None):
        text = value if value is not None else self._lineedit.text()
        self.question.answer = usertypes.FileDownloadTarget(text)
        return True

    def download_open(self, cmdline):
        self.question.answer = usertypes.OpenFileDownloadTarget(cmdline)
        modeman.maybe_leave(self._win_id, usertypes.KeyMode.prompt,
                            'download open')
        self.question.done()

    def _allowed_commands(self):
        cmds = [
            ('prompt-accept', 'Accept'),
            ('leave-mode', 'Abort'),
            ('prompt-open-download', "Open download"),
        ]
        return cmds


class AuthenticationPrompt(_BasePrompt):

    """A prompt for username/password."""

    def __init__(self, question, win_id, parent=None):
        super().__init__(question, win_id, parent)
        self._init_title(question)

        user_label = QLabel("Username:", self)
        self._user_lineedit = LineEdit(self)

        password_label = QLabel("Password:", self)
        self._password_lineedit = LineEdit(self)
        self._password_lineedit.setEchoMode(QLineEdit.Password)

        grid = QGridLayout()
        grid.addWidget(user_label, 1, 0)
        grid.addWidget(self._user_lineedit, 1, 1)
        grid.addWidget(password_label, 2, 0)
        grid.addWidget(self._password_lineedit, 2, 1)
        self._vbox.addLayout(grid)
        self._init_key_label()

        assert not question.default, question.default
        self.setFocusProxy(self._user_lineedit)

    def accept(self, value=None):
        if value is not None:
            if ':' not in value:
                raise Error("Value needs to be in the format "
                            "username:password, but {} was given".format(
                                value))
            username, password = value.split(':', maxsplit=1)
            self.question.answer = AuthTuple(username, password)
            return True
        elif self._user_lineedit.hasFocus():
            # Earlier, tab was bound to :prompt-accept, so to still support
            # that we simply switch the focus when tab was pressed.
            self._password_lineedit.setFocus()
            return False
        else:
            self.question.answer = AuthTuple(self._user_lineedit.text(),
                                             self._password_lineedit.text())
            return True

    def item_focus(self, which):
        """Support switching between fields with tab."""
        assert which in ['prev', 'next'], which
        if which == 'next' and self._user_lineedit.hasFocus():
            self._password_lineedit.setFocus()
        elif which == 'prev' and self._password_lineedit.hasFocus():
            self._user_lineedit.setFocus()

    def _allowed_commands(self):
        return [('prompt-accept', "Accept"),
                ('leave-mode', "Abort")]


class YesNoPrompt(_BasePrompt):

    """A prompt with yes/no answers."""

    KEY_MODE = usertypes.KeyMode.yesno

    def __init__(self, question, win_id, parent=None):
        super().__init__(question, win_id, parent)
        self._init_title(question)
        self._init_key_label()

    def accept(self, value=None):
        if value is None:
            if self.question.default is None:
                raise Error("No default value was set for this question!")
            self.question.answer = self.question.default
        elif value == 'yes':
            self.question.answer = True
        elif value == 'no':
            self.question.answer = False
        else:
            raise Error("Invalid value {} - expected yes/no!".format(value))
        return True

    def _allowed_commands(self):
        cmds = [
            ('prompt-accept yes', "Yes"),
            ('prompt-accept no', "No"),
        ]

        if self.question.default is not None:
            assert self.question.default in [True, False]
            default = 'yes' if self.question.default else 'no'
            cmds.append(('prompt-accept', "Use default ({})".format(default)))

        cmds.append(('leave-mode', "Abort"))
        return cmds


class AlertPrompt(_BasePrompt):

    """A prompt without any answer possibility."""

    def __init__(self, question, win_id, parent=None):
        super().__init__(question, win_id, parent)
        self._init_title(question)
        self._init_key_label()

    def accept(self, value=None):
        if value is not None:
            raise Error("No value is permitted with alert prompts!")
        # Doing nothing otherwise
        return True

    def _allowed_commands(self):
        return [('prompt-accept', "Hide")]
