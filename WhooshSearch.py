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
_whoosh_search_settings = "WhooshSearch.sublime-settings"
_settings = sublime.load_settings(_whoosh_search_settings)
_whoosh_syntax_file = "Packages/WhooshSearch/WhooshFindResults.hidden-tmLanguage"
_find_in_files_name = "Whoosh Find Results"

STOP_WORDS = frozenset(('a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can',
                        'for', 'from', 'have', 'if', 'in', 'is', 'it', 'may',
                        'not', 'of', 'on', 'or', 'tbd', 'that', 'the', 'this',
                        'to', 'us', 'we', 'when', 'will', 'with', 'yet',
                        'you', 'your'))

sys.argv = [""]

current_milli_time = lambda: int(round(time.time() * 1000))

def CustomFancyAnalyzer(expression=r"\s+", stoplist=STOP_WORDS, minsize=2,
                  maxsize=None, gaps=True, splitwords=True, splitnums=True,
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


def custom_analyzer():
    return CustomFancyAnalyzer()

def get_schema():
    return Schema (path=ID(unique=True, stored=True),
                   time=STORED,
                   content=TEXT(analyzer=custom_analyzer(), chars=True, stored=True))


def is_hidden(filepath):
    name = os.path.basename(os.path.abspath(filepath))
    return name.startswith('.') or has_hidden_attribute(filepath)


def has_hidden_attribute(filepath):
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(filepath)
        assert attrs != -1
        result = bool(attrs & 2)
    except (AttributeError, AssertionError):
        result = False
    return result


def is_path_contains(path, directory):
    if directory in path.split(os.sep):
        return True
    return False


def file_content(file_path):
    with io.open(file_path, encoding="utf-8", errors="ignore") as f:
        content = f.read()
    return content


def is_binary_file(file_path):
    textchars = bytearray({7,8,9,10,12,13,27} | set(range(0x20, 0x100)) - {0x7f})
    is_binary_string = lambda bytes: bool(bytes.translate(None, textchars))
    with open(file_path, "rb") as f:
        is_bin = is_binary_string(f.read(1024))
    return is_bin


def add_doc_to_index(writer, fname):
    content = file_content(fname)
    time = os.path.getmtime(fname)
    writer.add_document(path=fname, content=content, time=time)



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


    def status_message(self, message, timeout=2000):
        if not message:
            self.status_message_id = 0
            return

        self.status_message_id = current_milli_time()
        threading.Thread(target=self.periodic_status_message, args=(message, self.status_message_id, timeout)).start()


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
        folder_name = os.path.split(dirpath)[1]

        for skip in skip_folders:
            if folder_name == skip:
                return True
        return False


    def file_filter(self, fname):
        if not os.path.isfile(fname):
            return False

        if is_hidden(fname):
            return False

        if is_binary_file(fname):
            return False

        # for test artemn
        if self.project_name() == "/home/artemn/linux/linux_4_3_3.sublime-project" and \
           os.path.splitext(fname)[1] not in [".c", ".h"]:
            return False

        return True


    def dir_filter(self, dirpath):
        if is_hidden(dirpath):
            return False

        if self.skip_dir(dirpath):
            return False

        return True


class WhooshIndex(WhooshInfrastructure):
    def __init__(self, window):
        WhooshInfrastructure.__init__(self, window)

    def __call__(self):
        start = timeit.default_timer()
        if not self.is_project():
            print("WhooshSearch indexes only projects")
            return

        index_path = self.prepare_index_folder()

        try:
            if not index.exists_in(index_path):
                self.new_index(index_path)
            else:
                self.incremental_index(index_path)

            stop = timeit.default_timer()
            print(stop - start)
        except index.LockError:
            print("Index is locked")


    # Create the index from scratch
    def new_index(self, index_path):
        ix = index.create_in(index_path, schema=get_schema())
        with ix.writer(limitmb=2048) as writer:
            file_count = 0
            for fname in self.project_files():
                add_doc_to_index(writer, fname)
                file_count += 1
                if file_count and file_count % 100 == 0:
                    self.status_message("Whoosh Indexing: %d" % file_count)
            self.status_message("Whoosh Commit: %d" % file_count)

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
            with ix.writer(limitmb=2048) as writer:
                # Loop over the stored fields in the index
                for fields in searcher.all_stored_fields():
                    indexed_path = fields['path']
                    # print("artemn: WAS INDEXED: %s" % indexed_path)
                    indexed_paths.add(indexed_path)

                    if not os.path.exists(indexed_path) or not self.file_filter(indexed_path):
                        # This file was deleted since it was indexed
                        # print("artemn: This file was deleted since it was indexed")
                        writer.delete_by_term('path', indexed_path)
                    else:
                        # Check if this file was changed since it
                        # was indexed
                        indexed_time = fields['time']
                        mtime = os.path.getmtime(indexed_path)
                        if mtime > indexed_time:
                            # The file has changed, delete it and add it to the list of
                            # files to reindex
                            # print("artemn: file was changed")
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
                        # print("artemn: add to index: %s" % path)
                        add_doc_to_index(writer, path)
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
            print("WhooshSearch indexes only projects")
            return

        index_path = self.prepare_index_folder()
        try:
            self.new_index(index_path)

            stop = timeit.default_timer()
            print(stop - start)
        except index.LockError:
            print("Index is locked")


class WhooshSearch(WhooshInfrastructure):
    def __init__(self, window, search_string):
        WhooshInfrastructure.__init__(self, window)
        self.search_string = search_string
        self.whoosh_view = None

    def __call__(self):
        start = timeit.default_timer()

        if not self.is_project():
            print("WhooshSearch searches only projects")
            return

        if not index.exists_in(self.index_folder()):
            print("WhooshSearch: Please create the index")
            return

        ix = index.open_dir(self.index_folder())
        qp = QueryParser("content", schema=ix.schema)

        # Search for phrases. search_string should to be in quotes
        q = qp.parse('"%s"' % self.search_string)

        with ix.searcher() as searcher:
            hits = searcher.search(q, limit=None, terms=True)
            self.show_hits(hits)

        stop = timeit.default_timer()
        print(stop - start)


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
        text = "\n%d matches across %d files\n" % (len(regions) - 1, hit_count)
        self.whoosh_view.run_command("whoosh_view_append_text",
                                     {"text" : text, "search_string" : None})


    def show_hits(self, hits):
        self.setup_hits(hits)
        self.open_whoosh_view()
        self.clear_whoosh_view()

        self.display_header(hits.searcher.doc_count())

        for hit in hits:
            # content = file_content(hit["path"])
            content = hit["content"]
            fragments = hit.highlights("content", text=content, top=1)
            self.display_filepath(hit["path"])
            self.display_fragments(fragments)

        self.display_footer(len(hits))
        self.block_view()

    def block_view(self):
        self.whoosh_view.set_read_only(True)


class WhooshTest(WhooshInfrastructure):
    def __init__(self, window):
        WhooshInfrastructure.__init__(self, window)

    def __call__(self):
        self.status_message("Artemn!!!")
        time.sleep(10)
        self.status_message("Sveta!!!")
        time.sleep(10)
        self.status_message("")
        print("The End!")


###############################################################################

class WhooshIndexCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        whoosh_index = WhooshIndex(sublime.active_window())
        sublime.set_timeout_async(whoosh_index, 0)


class WhooshSearchPromptCommand(sublime_plugin.WindowCommand):
    def run(self):
        self.window.show_input_panel("Whoosh Search:", "", self.on_done, None, None)
        pass

    def on_done(self, text):
        try:
            self.window.run_command("whoosh_search", {"search_string": text} )
        except ValueError:
            pass


class WhooshSearchCommand(sublime_plugin.WindowCommand):
    def run(self, search_string):
        whoosh_search = WhooshSearch(self.window, search_string)
        sublime.set_timeout_async(whoosh_search, 0)


class WhooshResetCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        whoosh_reset = WhooshReset(sublime.active_window())
        sublime.set_timeout_async(whoosh_reset, 0)


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
    def run_(self, edit, args):
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
            file_line_number = self.line_number_from_match(line)
            file_name = self.file_name_from_match(line_region)
            print("%s: %s" % (file_name, file_line_number))
        else:
            print("PATH!")

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



class WhooshTestCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        start = timeit.default_timer()

        whoosh_test = WhooshTest(sublime.active_window())
        sublime.set_timeout_async(whoosh_test, 0)

        stop = timeit.default_timer()
        print(stop - start)

