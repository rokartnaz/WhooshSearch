import sublime
import sublime_plugin
import os
import io
import errno
import timeit
import time
import ctypes
import time
import shutil
import threading

import WhooshSearch.whoosh.analysis

from WhooshSearch.whoosh import index
from WhooshSearch.whoosh.fields import *
from WhooshSearch.whoosh.filedb.filestore import FileStorage
from WhooshSearch.whoosh.qparser import QueryParser
from WhooshSearch.whoosh.analysis \
    import RegexTokenizer, IntraWordFilter, LowercaseFilter, StopFilter, MultiFilter, FancyAnalyzer
from WhooshSearch.whoosh.compat import u
from WhooshSearch.whoosh import highlight
from WhooshSearch.whoosh.analysis import Token
from WhooshSearch.whoosh.query import Phrase
from itertools import groupby
import multiprocessing


_index_folder_tag = ".whoosh"
_whoosh_syntax_file = "Packages/WhooshSearch/WhooshFindResults.hidden-tmLanguage"
_find_in_files_name = "Whoosh Find Results"

STOP_WORDS = frozenset(('a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can',
                        'for', 'from', 'have', 'if', 'in', 'is', 'it', 'may',
                        'not', 'of', 'on', 'or', 'tbd', 'that', 'the', 'this',
                        'to', 'us', 'we', 'when', 'will', 'with', 'yet',
                        'you', 'your'))

sys.argv = [""]

current_milli_time = lambda: int(round(time.time() * 1000))

# Whoosh settings
_whoosh_search_settings = "WhooshSearch.sublime-settings"
_settings = sublime.load_settings(_whoosh_search_settings)


class WhooshSearchHistory():
    def __init__(self, size):
        self.size = size
        self.pos = 0
        self.history = []

    def down(self):
        self.pos += 1
        if self.pos > len(self.history) - 1:
            self.pos = len(self.history) - 1

    def up(self):
        self.pos -= 1
        if self.pos < 0:
            self.pos = 0

    def get(self):
        if not self.history:
            return None
        return self.history[self.pos]

    def add(self, search_string):
        self.history.append(search_string)
        if len(self.history) > self.size:
            self.history = self.history[1:]
        self.pos = len(self.history) - 1


_search_history = WhooshSearchHistory(100)


def CustomFancyAnalyzer(expression=r"\s+", stoplist=STOP_WORDS, minsize=2,
                  maxsize=None, gaps=True, splitwords=False, splitnums=False,
                  mergewords=False, mergenums=False):
    """Composes a RegexTokenizer with an IntraWordFilter, LowercaseFilter, and
    StopFilter.

    >>> ana = FancyAnalyzer()
    >>> [token.text for token in ana("Should I call getInt or get_real?")]
    ["should", "call", "getInt", "get", "int", "get_real", "get", "real"]

    :param expression: The regular expression pattern to use to extract tokens.
    :param stoplist: A list of stop words. Set this to None to disable
        the stop word filter.
    :param minsize: Words smaller than this are removed from the stream.
    :param maxsize: Words longer that this are removed from the stream.
    :param gaps: If True, the tokenizer *splits* on the expression, rather
        than matching on the expression.
    """

    return (RegexTokenizer(expression=expression, gaps=gaps)
            | IntraWordFilter(delims=u("-'\"()!@#$%^&*[]{}<>\|;:,./?`~=+"),
                splitwords=splitwords, splitnums=splitnums,
                              mergewords=mergewords, mergenums=mergenums)
            | LowercaseFilter()
            | StopFilter(stoplist=stoplist, minsize=minsize)
            )


class CustomHighlighter(highlight.Highlighter):
    # returns list of tuples (sublime.location, found_text_line)
    def highlight_hit(self, hitobj, fieldname, text=None, top=3, minscore=1):
        results = hitobj.results
        schema = results.searcher.schema
        field = schema[fieldname]
        to_bytes = field.to_bytes
        from_bytes = field.from_bytes

        # Get the terms searched for/matched in this field
        bterms = results.query_terms(expand=True, fieldname=fieldname)

        # Convert bytes to unicode
        words = frozenset(from_bytes(term[1]) for term in bterms)

        # Build the docnum->[(startchar, endchar),] map
        if fieldname not in results._char_cache:
            self._load_chars(results, fieldname, words, to_bytes)

        hitterms = (from_bytes(term[1]) for term in hitobj.matched_terms()
                    if term[0] == fieldname)

        # Grab the word->[(startchar, endchar)] map for this docnum
        cmap = results._char_cache[fieldname][hitobj.docnum]

        # A list of Token objects for matched words
        tokens = []
        charlimit = self.fragmenter.charlimit
        for word in hitterms:
            chars = cmap[word]
            for pos, startchar, endchar in chars:
                if charlimit and endchar > charlimit:
                    break
                tokens.append(Token(text=word, pos=pos,
                                    startchar=startchar, endchar=endchar))

        tokens.sort(key=lambda t: t.startchar)
        tokens = [max(group, key=lambda t: t.endchar - t.startchar)
                  for key, group in groupby(tokens, lambda t: t.startchar)]
        fragments = self.fragmenter.fragment_matches(text, tokens, words)

        # output = []
        # for frag in fragments:
        #     output.append( frag.text[frag.startchar:frag.endchar])
        return fragments

class CustomPinpointFragmenter(highlight.PinpointFragmenter):
    """This is a NON-RETOKENIZING fragmenter. It builds fragments from the
    positions of the matched terms.
    """

    #extract line containing all words from searching phrase
    def fragment_matches(self, text, tokens, words):
        j = -1

        for i, t in enumerate(tokens):
            if j >= i:
                continue
            j = i
            left = t.startchar
            right = t.endchar

            while text[left] != '\n':
                left -= 1
                if left == 0:
                    break

            if left != 0:
                left += 1

            while text[right] != '\n' and text[right] != '\r':
                right += 1
                if right == len(text):
                    break

            while j < len(tokens) - 1:
                next = tokens[j + 1]
                ec = next.endchar
                if ec <= right:
                    j += 1
                else:
                    break

            # check that whole phrase is here
            if j - i + 1 < len(words):
                continue

            token_dict = {t.text: 1 for t in tokens[i:j + 1]}
            if len(token_dict) < len(words):
                continue

            fragment = highlight.Fragment(text, tokens[i:j + 1], left, right)
            yield fragment


class CustomFormatter(highlight.Formatter):
    # Does not add anything to found text

    def format_token(self, text, token, replace=False):
        # Use the get_text function to get the text corresponding to the
        # token
        tokentext = highlight.get_text(text, token, replace)

        # Return the text as you want it to appear in the highlighted
        # string
        return tokentext


# WhooshInfrastructure incapsulates sublime.window where commands were started
class WhooshInfrastructure():
    def __init__(self, window):
        self.window = window
        self.status_message_id = 0

    def __call__(self):
        raise NotImplementedError

    def project_folders(self):
        return self.window.folders()


    def project_path(self):
        return self.window.extract_variables()['project_path']


    def project_name(self):
        return self.window.project_file_name()


    def is_project(self):
        if not self.project_name():
            return False
        return True


    def status_message(self, message, timeout=1500):
        if not message:
            self.status_message_id = 0
            return

        self.status_message_id = current_milli_time()
        threading.Thread(target=self.periodic_status_message,
                         args=(message, self.status_message_id, timeout)).start()


    def periodic_status_message(self, message, message_id, timeout=1000):
        while True:
            if message_id != self.status_message_id:
                return

            if not self.window:
                return

            self.window.status_message(message)
            time.sleep(timeout / 1000)


    #get list of all files in project that we are going to index
    #TODO return dictionary for quick search
    def project_files(self):
        for folder in self.project_folders():
            folder_files = []
            for (dirpath, dirnames, filenames) in os.walk(folder, topdown=True):
                dirnames[:] = [d for d in dirnames if self.dir_filter(d)]
                for f in filenames:
                    fname = os.path.join(dirpath, f)
                    if self.file_filter(fname):
                        yield fname


    def prepare_index_folder(self):
        index_path = self.project_name() + _index_folder_tag
        try:
            os.makedirs(index_path)
            return index_path
        except OSError as exception:
            if exception.errno != errno.EEXIST:
                raise
            # already exists
            return index_path


    def index_folder(self):
        return self.project_name() + _index_folder_tag


    def skip_dir(self, dirpath):
        skip_folders = _settings.get("skip_folders")

        if not skip_folders:
            return False

        folder_name = os.path.split(dirpath)[1]

        for skip in skip_folders:
            if folder_name == skip:
                return True
        return False


    def skip_file(self, fname):
        skip_files = _settings.get("skip_files")

        if not skip_files:
            return False

        if os.path.split(fname)[1] in skip_files:
            return True

        return False


    def skip_file_ext(self, fname):
        skip_exts = _settings.get("skip_file_extensions")

        if not skip_exts:
            return False

        ext = os.path.splitext(fname)[1]

        if ext[1:] in skip_exts:
            return True

        return False


    def file_filter(self, fname):
        if not os.path.isfile(fname):
            return False

        if self.is_hidden(fname):
            return False

        if self.is_binary_file(fname):
            return False

        if self.skip_file(fname):
            return False

        if self.skip_file_ext(fname):
            return False

        # for test artemn
        if self.project_name() == "/home/artemn/linux/linux_4_3_3.sublime-project" and \
           os.path.splitext(fname)[1] not in [".c", ".h"]:
            return False

        return True


    def dir_filter(self, dirpath):
        if self.is_hidden(dirpath):
            return False

        if self.skip_dir(dirpath):
            return False

        return True

    def get_schema(self):
        if _settings.get("store_content", False):
            return Schema (path=ID(unique=True, stored=True),
                           time=STORED,
                           content=TEXT(analyzer=CustomFancyAnalyzer(), chars=True, stored=True))
        else:
            return Schema (path=ID(unique=True, stored=True),
                           time=STORED,
                           content=TEXT(analyzer=CustomFancyAnalyzer(), chars=True))


    def is_hidden(self, filepath):
        name = os.path.basename(os.path.abspath(filepath))
        return name.startswith('.') or self.has_hidden_attribute(filepath)


    def has_hidden_attribute(self, filepath):
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(filepath)
            assert attrs != -1
            result = bool(attrs & 2)
        except (AttributeError, AssertionError):
            result = False
        return result

    def file_content(self, file_path):
        with io.open(file_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return content


    def is_binary_file(self, file_path):
        textchars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)) - {0x7f})
        is_binary_string = lambda bytes: bool(bytes.translate(None, textchars))
        with open(file_path, "rb") as f:
            is_bin = is_binary_string(f.read(1024))
        return is_bin


    def add_doc_to_index(self, writer, fname):
        content = self.file_content(fname)
        time = os.path.getmtime(fname)
        writer.add_document(path=fname, content=content, time=time)


class WhooshIndex(WhooshInfrastructure):
    def __init__(self, window):
        WhooshInfrastructure.__init__(self, window)

    def __call__(self):
        start = timeit.default_timer()
        if not self.is_project():
            self.window.status_message("WhooshSearch indexes only projects")
            return

        index_path = self.prepare_index_folder()

        try:
            if not index.exists_in(index_path):
                self.new_index(index_path)
            else:
                self.incremental_index(index_path)

            stop = timeit.default_timer()
            print("WhooshIndex finished [%f s]" % (stop - start))
        except index.LockError:
            self.window.status_message("WhooshSearch: Index is locked")


    # Create the index from scratch
    def new_index(self, index_path):
        self.status_message("Whoosh Indexing...")

        ix = index.create_in(index_path, schema=self.get_schema())
        with ix.writer(limitmb=_settings.get("ram_limit_mb", 1024)) as writer:
            file_count = 0
            for fname in self.project_files():
                self.add_doc_to_index(writer, fname)
                file_count += 1
                if file_count and file_count % 100 == 0:
                    self.status_message("Whoosh Indexing: %d" % file_count)
            self.status_message("Whoosh Commiting: %d" % file_count)

        self.status_message("")

        return ix


    # TODO should consider changes of settings to filter files and folders
    def incremental_index(self, index_path):
        ix = index.open_dir(index_path)

        # The set of all paths in the index
        indexed_paths = set()
        # The set of all paths we need to re-index
        to_index = set()

        self.status_message("Whoosh Indexing...")

        with ix.searcher() as searcher:
            with ix.writer(limitmb=_settings.get("ram_limit_mb", 1024)) as writer:
                # Loop over the stored fields in the index
                for fields in searcher.all_stored_fields():
                    indexed_path = fields['path']
                    indexed_paths.add(indexed_path)

                    if not os.path.exists(indexed_path) or not self.file_filter(indexed_path):
                        # This file was deleted since it was indexed
                        writer.delete_by_term('path', indexed_path)
                    else:
                        # Check if this file was changed since it
                        # was indexed
                        indexed_time = fields['time']
                        mtime = os.path.getmtime(indexed_path)
                        if mtime > indexed_time:
                            # The file has changed, delete it and add it to the list of
                            # files to reindex
                            writer.delete_by_term('path', indexed_path)
                            to_index.add(indexed_path)

                # Loop over the files in the filesystem
                # Assume we have a function that gathers the filenames of the
                # documents to be indexed
                file_count = 0
                for path in self.project_files():
                    if path in to_index or path not in indexed_paths:
                        # This is either a file that's changed, or a new file
                        # that wasn't indexed before. So index it!
                        self.add_doc_to_index(writer, path)
                        file_count += 1
                    if file_count and file_count % 100 == 0:
                        self.status_message("Whoosh Indexing: %d" % file_count)
                self.status_message("Whoosh Commit: %d" % file_count)
            self.status_message("")

        return ix


class WhooshReset(WhooshIndex):
    def __init__(self, window):
        WhooshIndex.__init__(self, window)

    def __call__(self):
        start = timeit.default_timer()
        if not self.is_project():
            self.window.status_message("WhooshSearch indexes only projects")
            return

        index_path = self.prepare_index_folder()
        try:
            self.new_index(index_path)

            stop = timeit.default_timer()
            print("WhooshReset finished [%f s]" % (stop - start))
        except index.LockError:
            self.window.status_message("WhooshSearch: Index is locked")


class WhooshSave(WhooshInfrastructure):
    def __init__(self, window, file_name):
        WhooshInfrastructure.__init__(self, window)
        self.file_name = file_name

    def __call__(self):
        if not self.is_project():
            self.window.status_message("WhooshSearch indexes only projects")
            return

        if self.belongs_project(self.file_name):
            self.reindex()

    def belongs_project(self, file_name):
        result = False

        fdir, fname = os.path.split(file_name)

        for folder in self.project_folders():
            if fdir.startswith(folder):
                result = True
                fdir = fdir[len(folder) + 1:]
                break

        if not result:
            return False


        while fdir:
            if self.dir_filter(fdir):
                fdir = os.path.split(fdir)[0]
            else:
                return False

        if not self.file_filter(file_name):
            return False

        return True

    def reindex(self):
        index_path = self.prepare_index_folder()
        ix = index.open_dir(index_path)

        try:
            with ix.searcher() as searcher:
                with ix.writer(limitmb=_settings.get("ram_limit_mb", 1024)) as writer:
                    fields = searcher.document(path=self.file_name)

                    if fields:
                        if os.path.getmtime(self.file_name) == fields['time']:
                            #nothing to reindex
                            self.window.status_message("Whoosh Saving: nothing to save")
                            return

                    writer.delete_by_term('path', self.file_name)
                    self.add_doc_to_index(writer, self.file_name)

                    self.status_message("Whoosh Saving: %s" % os.path.split(self.file_name)[1])

                self.status_message("")

        except index.LockError:
            self.window.status_message("WhooshSearch: Index is locked")


class WhooshSearch(WhooshInfrastructure):
    def __init__(self, window, search_string):
        WhooshInfrastructure.__init__(self, window)
        self.search_string = search_string
        self.whoosh_view = None

    def __call__(self):
        start = timeit.default_timer()

        if not self.is_project():
            self.window.status_message("WhooshSearch searches only projects")
            return

        if not index.exists_in(self.index_folder()):
            self.window.status_message("WhooshSearch: Please create the index")
            return

        ix = index.open_dir(self.index_folder())
        qp = QueryParser("content", schema=ix.schema)

        # Search for phrases. search_string should to be in quotes
        q = qp.parse('"%s"' % self.search_string)

        with ix.searcher() as searcher:
            hits = searcher.search(q, limit=None, terms=True)
            self.show_hits(hits)

        stop = timeit.default_timer()
        print("WhooshSearch finished [%f s]" % (stop - start))


    def setup_hits(self, hits):
        hits.highlighter = CustomHighlighter()
        hits.fragmenter = CustomPinpointFragmenter()
        hits.fragmenter.charlimit = None
        hits.formatter = CustomFormatter()


    def open_whoosh_view(self):
        tmp_view = next((v for v in self.window.views() if v.name() == _find_in_files_name), None)

        if tmp_view is not None:
            self.whoosh_view = tmp_view
            self.whoosh_view.set_read_only(False)
            self.window.focus_view(self.whoosh_view)
            self.whoosh_view.show(self.whoosh_view.size())
            return

        self.whoosh_view = self.window.new_file()
        self.whoosh_view.set_scratch(True)
        self.whoosh_view.set_name(_find_in_files_name)
        self.whoosh_view.set_syntax_file(_whoosh_syntax_file)
        return


    def clear_whoosh_view(self):
        self.whoosh_view.run_command("whoosh_view_clear_all")


    def display_filepath(self, filepath):
        self.whoosh_view.run_command("whoosh_view_append_text",
                                     {"text" : "\n%s:\n" % filepath, "search_string" : None})


    def display_fragments(self, fragments):
        line_count_start = 0
        line_count = 0
        for fragment in fragments:
            line_count += fragment.text.count('\n', line_count_start, fragment.startchar)
            text = '%8d:\t' % (line_count + 1)
            text += fragment.text[fragment.startchar : fragment.endchar] + '\n';
            line_count_start = fragment.startchar
            self.whoosh_view.run_command("whoosh_view_append_text",
                                         {"text" : text, "search_string" : self.search_string})


    def display_header(self, file_number):
        header = 'Searching %d files for "%s"\n' % (file_number, self.search_string)
        self.whoosh_view.run_command("whoosh_view_append_text",
                                     {"text" : header, "search_string" : self.search_string})


    def display_footer(self, hit_count):
        regions = self.whoosh_view.find_all(self.search_string,
                                            sublime.LITERAL | sublime.IGNORECASE)
        reg_num = len(regions) - 1

        text = "\n%d matches across %d files\n" % (reg_num if reg_num >= 0 else 0, hit_count)
        self.whoosh_view.run_command("whoosh_view_append_text",
                                     {"text" : text, "search_string" : None})


    def show_hits(self, hits):
        self.setup_hits(hits)
        self.open_whoosh_view()
        self.clear_whoosh_view()

        self.display_header(hits.searcher.doc_count())

        for hit in hits:
            if _settings.get("store_content", False) and "content" in hit:
                content = hit["content"]
            else:
                content = self.file_content(hit["path"])

            fragments = hit.highlights("content", text=content, top=1)
            self.display_filepath(hit["path"])
            self.display_fragments(fragments)

        self.display_footer(len(hits))
        self.block_view()

    def block_view(self):
        self.whoosh_view.set_read_only(True)
        self.whoosh_view.sel().clear()


class WhooshTest(WhooshInfrastructure):
    def __init__(self, window):
        WhooshInfrastructure.__init__(self, window)

    def __call__(self):
        f = "C:\\artem\\texttxt"
        print(os.path.splitext(f)[1])



def jump_find_result(file_view, line_number, search_string):
    while file_view.is_loading():
        pass

    pos = file_view.text_point(line_number - 1, 0)
    file_view.show_at_center(pos)

    file_view.sel().clear()
    reg = file_view.find(search_string, pos, sublime.LITERAL | sublime.IGNORECASE)
    if reg.a != -1:
        file_view.sel().add(reg)
    else:
        file_view.sel().add(sublime.Region(pos, pos))


def refresh_whoosh_input(search_string):
    sublime.active_window().run_command("whoosh_search_prompt",
            {"search_string" : search_string})

###############################################################################

class WhooshIndexCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        whoosh_index = WhooshIndex(sublime.active_window())
        threading.Thread(target=whoosh_index).start()


class WhooshSearchPromptCommand(sublime_plugin.WindowCommand):
    def run(self, search_string=None):
        global _search_history
        curr_view = self.window.active_view()

        if not search_string:
            sel = curr_view.sel()
            if len(sel) == 0 or sel[0].a == sel[0].b:
                search_string = _search_history.get()
            else:
                search_string = curr_view.substr(sel[0])

            if not search_string:
                search_string = ""

        self.window.show_input_panel("Whoosh Search:", search_string, self.on_done, None, None)
        self.window.run_command("select_all")
        pass

    def on_done(self, text):
        global _search_history
        try:
            _search_history.add(text)
            self.window.run_command("whoosh_search", {"search_string": text} )
        except ValueError:
            pass


class WhooshSearchCommand(sublime_plugin.WindowCommand):
    def run(self, search_string):
        if not search_string:
            return

        whoosh_search = WhooshSearch(self.window, search_string)
        threading.Thread(target=whoosh_search).start()


class WhooshResetCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        whoosh_reset = WhooshReset(sublime.active_window())
        threading.Thread(target=whoosh_reset).start()


class WhooshEventListener(sublime_plugin.EventListener):
    def on_post_save(self, view):
        whoosh_save = WhooshSave(sublime.active_window(), view.file_name())
        threading.Thread(target=whoosh_save).start()


class WhooshViewAppendTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text, search_string):
        start_point = self.view.size()
        self.view.insert(edit, start_point, text)

        if search_string is not None:
            regions = self.view.find_all(search_string,
                                    sublime.LITERAL | sublime.IGNORECASE)
            self.view.add_regions('whoosh_regions', regions[1:], "text.find-in-files", "", sublime.DRAW_OUTLINED)


class WhooshViewClearAllCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.erase(edit, sublime.Region(0, self.view.size()))


class WhooshDoubleClickCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if self.view.name() == _find_in_files_name:
            self.whoosh_jump()
        else:
            self.view.run_command("expand_selection", {"to": "word"})

    def whoosh_jump(self):
        click_region = self.view.sel()[0]
        line_region = self.view.line(click_region)
        line = self.view.substr(line_region)

        if not line:
            return

        if line[0].isspace():
            # find line number and go up until Path is found
            line_number = self.line_number_from_match(line)
            file_name = self.file_name_from_match(line_region)
            search_string = self.get_search_string()
            self.jump_file(file_name, line_number, search_string)
        else:
            if line.split()[0] == "Searching":
                return
            self.jump_file(line[:-1], 0, "")

    def get_search_string(self):
        line_region = self.view.line(sublime.Region(0, 0))
        line = self.view.substr(line_region)
        i = 0
        while line[i] != '"':
            i += 1

        return line[i + 1 : len(line) - 1]

    def line_number_from_match(self, line):
        i = 0
        while line[i].isspace():
            i += 1

        left = i

        while line[i] != ':':
            i += 1

        right = i

        return int(line[left : right])

    def file_name_from_match(self, match_region):
        current_row = self.view.rowcol(match_region.a)[0]
        while True:
            current_row -= 1
            first_char = self.view.substr(self.view.text_point(current_row, 0))
            if not first_char:
                continue
            if not first_char.isspace():
                break

        line_region = self.view.line(self.view.text_point(current_row, 0))
        return self.view.substr(line_region)[:-1]

    def jump_file(self, file_name, line_number, search_string):
        file_view = sublime.active_window().open_file(file_name)
        threading.Thread(target=jump_find_result,
                         args=(file_view, line_number, search_string)).start()


class WhooshArrowUpCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global _search_history
        _search_history.up()
        search_string = _search_history.get()

        threading.Thread(target=refresh_whoosh_input,
                         args=(search_string,)).start()


class WhooshArrowDownCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global _search_history
        _search_history.down()
        search_string = _search_history.get()

        threading.Thread(target=refresh_whoosh_input,
                         args=(search_string,)).start()


class WhooshTestCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        start = timeit.default_timer()

        whoosh_test = WhooshTest(sublime.active_window())
        threading.Thread(target=whoosh_test).start()

        stop = timeit.default_timer()
        print(stop - start)

