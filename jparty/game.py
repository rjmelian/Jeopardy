from types import SimpleNamespace
from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import QInputDialog, QMessageBox, QApplication


# from PyQt6.QtMultimedia import QSound
import threading
import time
from dataclasses import dataclass
import pickle
import os
import sys
import simpleaudio as sa
from collections.abc import Iterable
import logging

from .constants import DEBUG
from .utils import SongPlayer, resource_path, CompoundObject

activation_time = 0


def rasync(f, *args, **kwargs):
    t = threading.Thread(target=f, args=args, kwargs=kwargs)
    t.start()


class QuestionTimer(object):
    def __init__(self, interval, f, *args, **kwargs):
        super().__init__()
        self.f = f
        self.args = args
        self.kwargs = kwargs
        self.interval = interval
        self.__thread = None
        self.__start_time = None
        self.__elapsed_time = 0

    def run(self, i):
        thread = self.__thread
        time.sleep(i)
        if thread == self.__thread:
            self.f(*self.args, **self.kwargs)

    def start(self):
        """wrapper for resume"""
        self.resume()

    def cancel(self):
        """wrapper for pause"""
        self.pause()

    def pause(self):
        self.__thread = None
        self.__elapsed_time += time.time() - self.__start_time

    def resume(self):
        self.__thread = threading.Thread(
            target=self.run, args=(self.interval - self.__elapsed_time,)
        )
        self.__thread.start()
        self.__start_time = time.time()


@dataclass
class KeystrokeEvent:
    key: int
    func: callable
    hint_setter: callable = None
    active: bool = False
    persistent: bool = False


class KeystrokeManager(object):
    def __init__(self):
        super().__init__()
        self.__events = {}

    def addEvent(
        self, ident, key, func, hint_setter=None, active=False, persistent=False
    ):
        self.__events[ident] = KeystrokeEvent(
            key, func, hint_setter, active, persistent
        )

    def call(self, key):
        """this is split in to two for loops so one execution doesnt cause another event to trigger"""
        events_to_call = []
        for ident, event in self.__events.items():
            if event.active and event.key == key:
                events_to_call.append(event)
                if not event.persistent:
                    self._deactivate(ident)

        for event in events_to_call:
            event.func()

    def _activate(self, ident):
        e = self.__events[ident]
        e.active = True
        e.hint_setter(True)
        if e.hint_setter:
            e.hint_setter(True)

    def _deactivate(self, ident):
        e = self.__events[ident]
        e.active = False
        if e.hint_setter:
            e.hint_setter(False)

    def activate(self, *idents):
        if isinstance(idents, Iterable):
            for ident in idents:
                self._activate(ident)
        else:
            self._activate(idents)

    def deactivate(self, *idents):
        if isinstance(idents, Iterable):
            for ident in idents:
                self._deactivate(ident)
        else:
            self._deactivate(idents)




class Question(object):
    def __init__(self, index, text, answer, value, dd=False):
        self.index = index
        self.text = text
        self.answer = answer
        self.value = value
        self.dd = dd
        self.complete = False


class Board(object):
    def __init__(self, categories, questions, final=False, dj=False):
        logging.info(f"{len(questions)} questions in round")
        self.complete = len(questions) == 30 if not final else len(questions) == 1
        if final:
            self.size = (1, 1)
        else:
            self.size = (6, 5)
        self.final = final
        self.categories = categories
        self.dj = dj
        if not questions is None:
            self.questions = questions
        else:
            self.questions = []

    def get_question(self, i, j):
        for q in self.questions:
            if q.index == (i, j):
                return q
        return None


def updateUI(f):
    def wrapper(self, *args):
        ret = f(self, *args)
        self.update()
        return ret

    return wrapper



@dataclass
class GameData:
    rounds: list
    date: str
    comments: str

class Game(QObject):
    buzz_trigger = pyqtSignal(int)
    new_player_trigger = pyqtSignal()
    # buzzer_disconnected = pyqtSignal(str)
    wager_trigger = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()

        self.host_display = None
        self.main_display = None
        self.socket_controller = None
        self.dc = None

        self.song_player = SongPlayer()

        self.data = None

        # self.rounds = []
        # self.date = ""
        # self.comments = ""
        self.current_round = None
        self.players = []

        self.paused = False
        self.active_question = None
        self.accepting_responses = False
        self.answering_player = None
        self.previous_answerer = None
        self.timer = None

        # self.song = QSound('data:song.wav')
        self.song_player = SongPlayer()
        self.__judgement_round = -1
        self.__judgement_subround = 2
        self.__sorted_players = None
        self.wagered = set()

        self.buzzer_controller = None

        self.keystroke_manager = KeystrokeManager()
        self.keystroke_manager.addEvent(
            "CORRECT_RESPONSE",
            Qt.Key.Key_Left,
            self.correct_answer,
            self.arrowhints,
        )
        self.keystroke_manager.addEvent(
            "INCORRECT_RESPONSE",
            Qt.Key.Key_Right,
            self.incorrect_answer,
            self.arrowhints,
        )
        self.keystroke_manager.addEvent(
            "BACK_TO_BOARD", Qt.Key.Key_Space, self.back_to_board, self.spacehints
        )
        self.keystroke_manager.addEvent(
            "OPEN_RESPONSES", Qt.Key.Key_Space, self.open_responses, self.spacehints
        )
        self.keystroke_manager.addEvent(
            "NEXT_ROUND", Qt.Key.Key_Space, self.next_round, self.spacehints
        )
        self.keystroke_manager.addEvent(
            "NEXT_SLIDE",
            Qt.Key.Key_Space,
            self.final_next_slide,
            self.spacehints,
            persistent=True,
        )
        self.keystroke_manager.addEvent(
            "OPEN_FINAL", Qt.Key.Key_Space, self.open_final, self.spacehints
        )
        self.keystroke_manager.addEvent(
            "CLOSE_GAME", Qt.Key.Key_Space, self.close_game, self.spacehints
        )

        # if DEBUG:
        #     self.completed_questions = self.rounds[1].questions[:-1]

        self.wager_trigger.connect(self.wager)
        self.buzz_trigger.connect(self.buzz)
        self.new_player_trigger.connect(self.new_player)



    def startable(self):
        if DEBUG:
            return True
        return (
            self.valid()
            and len(self.socket_controller.connected_players) > 0
        )

    def begin(self):
        if not DEBUG:
            self.song_player.play(repeat=True)
        else:
            self.song_player = None

    def start_game(self):
        self.current_round = self.data.rounds[1]
        self.dc.hide_welcome_widgets()
        self.dc.board_widget.load_round(self.current_round)
        self.socket_controller.accepting_players = False
        if not DEBUG:
            self.song_player.stop()

    def setDisplays(self, host_display, main_display):
        self.host_display = host_display
        self.main_display = main_display
        self.dc = CompoundObject(host_display, main_display)

    def setSocketController(self, controller):
        self.socket_controller = controller

    def arrowhints(self, val):
        self.dc.borders.arrowhints(val)

    def spacehints(self, val):
        self.dc.borders.spacehints(val)

    def update(self):
        pass
        # self.dc.update()

    def new_player(self):
        self.players = self.socket_controller.connected_players
        self.dc.scoreboard.refresh_players()
        self.host_display.welcome_widget.check_start()

    def valid(self):
        return self.data is not None and all(b.complete for b in self.data.rounds)

    def __accept_responses(self):
        self.accepting_responses = True
        # global activation_time
        # activation_time = time.time()

    @updateUI
    def open_responses(self):
        logging.info("open responses")
        self.dc.borders.lights(True)
        if self.current_round.final:
            self.buzzer_controller.prompt_answers()

            if DEBUG:
                FJTIME = 1
            else:
                FJTIME = 31
                self.song_player.final()
            self.timer = QuestionTimer(FJTIME, self.stumped)
        else:
            self.__accept_responses()

            if not self.timer:
                self.timer = QuestionTimer(4, self.stumped)
        self.timer.start()

    @updateUI
    def close_responses(self):
        logging.info("close responses")
        self.timer.pause()
        self.accepting_responses = False
        self.dc.borders.lights(True)

    def buzz(self, i_player):
        player = self.players[i_player]
        if self.accepting_responses and player is not self.previous_answerer:
            logging.info(f"buzz ({time.time() - activation_time:.6f} s)")
            self.accepting_responses = False
            self.timer.pause()
            self.previous_answerer = player
            self.dc.player_widget(player).run_lights()

            self.answering_player = player
            self.keystroke_manager.activate("CORRECT_RESPONSE", "INCORRECT_RESPONSE")
            self.dc.borders.lights(False)
            self.update()
        elif self.active_question is None:
            self.dc.player_widget(player).buzz_hint()
        else:
            pass

    def answer_given(self):
        logging.info("answer given")
        if self.current_round.final:
            self.final_next_slide()
            self.keystroke_manager.activate("NEXT_SLIDE")
            return

        self.dc.player_widget(self.answering_player).stop_lights()
        self.keystroke_manager.deactivate("CORRECT_RESPONSE", "INCORRECT_RESPONSE")
        self.answering_player = None

    @updateUI
    def back_to_board(self):
        logging.info("back_to_board")
        self.dc.hide_question()
        self.timer = None
        self.active_question.complete = True
        self.active_question = None
        self.previous_answerer = None
        rasync(self.save)
        if all(q.complete for q in self.current_round.questions):
            self.keystroke_manager.activate("NEXT_ROUND")

    @updateUI
    def next_round(self):
        logging.info("next round")
        self.__current_round += 1
        # self.completed_questions = self.rounds[self.__current_round].questions[:-1]  # EDIT
        if self.__current_round == 2:
            self.start_final()

    def start_final(self):
        self.buzzer_controller.open_wagers()

    @updateUI
    def wager(self, i_player, amount):
        player = self.players[i_player]
        player.wager = amount
        self.wagered.add(player)
        logging.info(f"{player.name} wagered {amount}")
        if len(self.wagered) == len(self.players):
            self.keystroke_manager.activate("OPEN_FINAL")

    def answer(self, player, guess):
        player.finalanswer = guess
        logging.info(f"{player.name} guessed {guess}")

    @updateUI
    def final_next_slide(self):
        logging.info("NEXT SLIDE")
        if self.__judgement_round == -1:
            self.dc.finalanswerwindow.setVisible(True)
            self.__sorted_players = sorted(self.players, key=lambda x: x.score)

        if self.__judgement_subround == 2:
            if self.__judgement_round == len(self.players) - 1:
                self.end_game()
            else:
                self.__judgement_subround = 0
                self.__judgement_round += 1
                self.answering_player = self.__sorted_players[self.__judgement_round]
        else:
            self.__judgement_subround += 1

        self.dc.finalanswerwindow.info_level = self.__judgement_subround

        if self.__judgement_subround == 1:
            self.keystroke_manager.deactivate("NEXT_SLIDE")
            self.keystroke_manager.activate("CORRECT_RESPONSE", "INCORRECT_RESPONSE")

    @updateUI
    def end_game(self):
        winner = max(self.players, key=lambda p: p.score)
        self.dc.finalanswerwindow.winner = winner
        self.answering_player = winner
        self.keystroke_manager.deactivate("NEXT_SLIDE")
        self.keystroke_manager.activate("CLOSE_GAME")

    def close_game(self):
        self.main_display.close()
        self.host_display.close()
        self.buzzer_controller.restart()
        self.welcome_window.restart()

    @updateUI
    def run_dd(self):
        while True:
            player_name = QInputDialog.getItem(
                self.host_display,
                "Player selection",
                "Who found the Daily Double?",
                ["name" for p in self.players],
                editable=False,
            )[0]
            player = next((p for p in self.players if p.name == player_name), None)
            max_wager = max(player.score, 1000)
            wager_res = QInputDialog.getInt(
                self.host_display,
                "Wager",
                f"How much does {player_name} wager? (max: ${max_wager})",
                min=0,
                max=max_wager,
            )
            if wager_res[1]:
                break
        wager = wager_res[0]
        self.active_question.value = wager

        self.answering_player = player
        self.keystroke_manager.activate("CORRECT_RESPONSE", "INCORRECT_RESPONSE")
        self.dc.boardwidget.questionwidget.show_question()

    @updateUI
    def load_question(self, q):
        self.active_question = q
        if q.dd:
            logging.info("Daily double!")
            wo = sa.WaveObject.from_wave_file(resource_path("dd.wav"))
            wo.play()
            self.run_dd()
        else:
            self.keystroke_manager.activate("OPEN_RESPONSES")
        self.dc.load_question(q)

    @updateUI
    def open_final(self):
        self.dc.load_question(self.current_round.questions[0])
        self.keystroke_manager.activate("OPEN_RESPONSES")

    def save(self):
        # pickle.dump(self, open(".bkup",'wb'))
        pass

    @updateUI
    def correct_answer(self):
        logging.info("correct")
        ap = self.answering_player
        if self.current_round.final:
            ap.score += ap.wager
            self.set_score( ap, ap.score + ap.wager)
            self.answer_given()
            return

        if self.timer:
            self.timer.cancel()

        self.set_score( ap, ap.score + self.active_question.value)
        self.back_to_board()
        self.dc.borders.lights(False)

        self.answer_given()

    @updateUI
    def incorrect_answer(self):
        logging.info("incorrect")
        ap = self.answering_player
        if self.current_round.final:
            self.set_score( ap, ap.score - ap.value)
            self.answer_given()
            return

        self.set_score( ap, ap.score - self.active_question.value)
        self.answer_given()
        if self.active_question.dd:
            self.back_to_board()
        else:
            self.open_responses()
            self.timer.resume()

    @updateUI
    def stumped(self):
        logging.info("stumped")
        self.accepting_responses = False
        if not self.current_round.final:
            wo = sa.WaveObject.from_wave_file(resource_path("stumped.wav"))
            wo.play()

        # flash
        self.dc.borders.flash()
        if self.current_round.final:
            self.keystroke_manager.activate("NEXT_SLIDE")
        else:
            self.keystroke_manager.activate("BACK_TO_BOARD")

    def __getstate__(self):
        return (
            (self.rounds, self.date, self.comments),
            self.players
            # TODO: include completed questions
        )

    def __setstate__(self, state):
        self.new_game(*state[0])
        self.players = state[1]
        # self.completed_questions = state[2]


    def set_score(self, player, score):
        player.score = score
        self.dc.player_widget(player).update_score()

    def adjust_score(self, player):
        new_score, answered = QInputDialog.getInt(
            self.host_display,
            "Adjust Score",
            f"Enter a new score:",
            value=player.score,
        )
        if answered:
            self.set_score(player, new_score)

    def close(self):
        if not DEBUG:
            self.song_player.stop()
        QApplication.quit()



class Player(object):
    def __init__(self, name, waiter):
        self.name = name
        self.token = os.urandom(15)
        self.score = 0
        self.waiter = waiter
        self.wager = None
        self.finalanswer = ""
        self.page = "buzz"

    def __hash__(self):
        return int.from_bytes(self.token, sys.byteorder)


game_params = SimpleNamespace()
game_params.money1 = [200, 400, 600, 800, 1000]
game_params.money2 = [400, 800, 1200, 1600, 2000]
