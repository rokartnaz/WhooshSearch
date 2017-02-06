import sublime
import sublime_plugin
import os
import io
import errno
import timeit
import ctypes
import time

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


_index_folder_tag = ".whoosh"
_whoosh_search_settings = "WhooshSearch.sublime-settings"
_settings = None
_whoosh_syntax_file = "Packages/WhooshSearch/WhooshFindResults.hidden-tmLanguage"
_whoosh_view = None

STOP_WORDS = frozenset(('a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can',
                        'for', 'from', 'have', 'if', 'in', 'is', 'it', 'may',
                        'not', 'of', 'on', 'or', 'tbd', 'that', 'the', 'this',
                        'to', 'us', 'we', 'when', 'will', 'with', 'yet',
                        'you', 'your'))

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
                   content=TEXT(analyzer=custom_analyzer(), chars=True))


def project_folders():
    return sublime.active_window().folders()


def project_path():
    return sublime.active_window().extract_variables()['project_path']


def project_name():
    return sublime.active_window().project_file_name()


def is_project():
    if not project_name():
        return False
    return True


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


def skip_dir(dirpath):
    skip_folders = _settings.get("skip_folders")
    folder_name = os.path.split(dirpath)[1]

    for skip in skip_folders:
        if folder_name == skip:
            return True
    return False


def file_filter(fname):
    if not os.path.isfile(fname):
        return False

    if is_hidden(fname):
        return False

    if is_binary_file(fname):
        return False

    # for test artemn
    if os.path.splitext(fname)[1] not in [".c", ".h"]:
        return False

    return True


def dir_filter(dirpath):
    if is_hidden(dirpath):
        return False

    if skip_dir(dirpath):
        return False

    return True


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


#get list of all files in project that we are going to index
#TODO return dictionary for quick search
def project_files():
    for folder in project_folders():
        folder_files = []
        for (dirpath, dirnames, filenames) in os.walk(folder, topdown=True):
            dirnames[:] = [d for d in dirnames if dir_filter(d)]
            for f in filenames:
                fname = os.path.join(dirpath, f)
                if file_filter(fname):
                    yield fname


def prepare_index_folder():
    index_path = project_name() + _index_folder_tag
    try:
        os.makedirs(index_path)
        return index_path
    except OSError as exception:
        if exception.errno != errno.EEXIST:
            raise
        # already exists
        return index_path


def index_folder():
    return project_name() + _index_folder_tag


def add_doc_to_index(writer, fname):
    content = file_content(fname)
    time = os.path.getmtime(fname)
    writer.add_document(path=fname, content=content, time=time)


# Create the index from scratch
def new_index(index_path):
    ix = index.create_in(index_path, schema=get_schema())
    with ix.writer(procs=4, limitmb=512, multisegment=True) as writer:
        file_count = 0
        for fname in project_files():
            add_doc_to_index(writer, fname)
            file_count += 1
            if file_count % 100 == 0:
                sublime.active_window().status_message("Whoosh Indexing: %d" % file_count)
        sublime.active_window().status_message("Whoosh Indexing: %d" % file_count)

    return ix


# TODO should consider changes of settings to filter files and folders
def incremental_index(index_path):
    ix = index.open_dir(index_path)

    # The set of all paths in the index
    indexed_paths = set()
    # The set of all paths we need to re-index
    to_index = set()

    print("artemn: increamental index")

    with ix.searcher() as searcher:
        with ix.writer() as writer:
            # Loop over the stored fields in the index
            for fields in searcher.all_stored_fields():
                indexed_path = fields['path']
                indexed_paths.add(indexed_path)

                if not os.path.exists(indexed_path) or not file_filter(indexed_path):
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
            for path in project_files():
                if path in to_index or path not in indexed_paths:
                    # This is either a file that's changed, or a new file
                    # that wasn't indexed before. So index it!
                    print("artemn: add to index: %s" % path)
                    add_doc_to_index(writer, path)
                file_count += 1
                if file_count % 100:
                    sublime.active_window().status_message("Whoosh Indexing: %d" % file_count)
            sublime.active_window().status_message("Whoosh Indexing: %d" % file_count)

    return ix


def display_search_results(whoosh_view, file_path, fragments):
     print("display_row_col")


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


def whoosh_reset():
    global _settings

    start = timeit.default_timer()
    if not is_project():
        print("WhooshSearch indexes only projects")
        return

    _settings = sublime.load_settings(_whoosh_search_settings)

    index_path = prepare_index_folder()
    try:
        new_index(index_path)

        stop = timeit.default_timer()
        print(stop - start)
    except index.LockError:
        print("Index is locked")


def whoosh_index():
    global _settings

    start = timeit.default_timer()
    if not is_project():
        print("WhooshSearch indexes only projects")
        return

    _settings = sublime.load_settings(_whoosh_search_settings)

    index_path = prepare_index_folder()

    try:
        if not index.exists_in(index_path):
            new_index(index_path)
        else:
            incremental_index(index_path)

        stop = timeit.default_timer()
        print(stop - start)
    except index.LockError:
        print("Index is locked")


def whoosh_search(search_string):
    start = timeit.default_timer()

    if not is_project():
        print("WhooshSearch searches only projects")
        return

    if not index.exists_in(index_folder()):
        print("WhooshSearch: Please create the index")
        return

    ix = index.open_dir(index_folder())
    print ("artemn : %s" % index_folder())
    qp = QueryParser("content", schema=ix.schema)

    # Search for phrases. search_string should to be in quotes
    q = qp.parse('"%s"' % search_string)

    with ix.searcher() as searcher:
        hits = searcher.search(q, limit=None, terms=True)
        print("artemn: found hits %d" % len(hits))
        show_hits(hits, search_string)

    stop = timeit.default_timer()
    print(stop - start)


def setup_hits(hits):
    hits.highlighter = CustomHighlighter()
    hits.fragmenter = CustomPinpointFragmenter()
    hits.fragmenter.charlimit = None
    hits.formatter = CustomFormatter()


def open_whoosh_view():
    global _whoosh_view
    view_index = sublime.active_window().get_view_index(_whoosh_view)

    if view_index[0] != -1:
        sublime.active_window().focus_view(_whoosh_view)
        _whoosh_view.show(_whoosh_view.size())
        return _whoosh_view

    _whoosh_view = sublime.active_window().new_file()
    _whoosh_view.set_scratch(True)
    _whoosh_view.set_name("Whoosh Find Results")
    _whoosh_view.set_syntax_file(_whoosh_syntax_file)
    #_whoosh_view.set_read_only(True)
    return _whoosh_view


def display_filepath(filepath):
    _whoosh_view.run_command("view_append_text",
                             {"text" : "%s:\n" % filepath, "search_string" : None})


def display_fragments(fragments, search_string):
    line_count_start = 0
    line_count = 0
    for fragment in fragments:
        line_count += fragment.text.count('\n', line_count_start, fragment.startchar)
        text = '%8d:\t' % (line_count + 1)
        text += fragment.text[fragment.startchar : fragment.endchar] + '\n';
        line_count_start = fragment.startchar
        _whoosh_view.run_command("view_append_text",
                                 {"text" : text, "search_string" : search_string})


def display_header(file_number, search_string):
    header = 'Searching %d files for "%s"\n\n' % (file_number, search_string)
    _whoosh_view.run_command("view_append_text",
                             {"text" : header, "search_string" : search_string})


def show_hits(hits, search_string):
    setup_hits(hits)
    open_whoosh_view()

    display_header(hits.searcher.doc_count(), search_string)

    for hit in hits:
        content = file_content(hit["path"])
        fragments = hit.highlights("content", text=content, top=1)
        display_filepath(hit["path"])
        display_fragments(fragments, search_string)


###############################################################################

class WhooshIndexCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        sublime.set_timeout_async(whoosh_index, 1)


class WhooshSearchCommand(sublime_plugin.TextCommand):
    def run(self, edit, search_string="pcb_va"):
        sublime.set_timeout_async(lambda: whoosh_search(search_string), 1)


class WhooshResetCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        sublime.set_timeout_async(whoosh_reset, 1)


class ViewAppendTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, text, search_string):
        start_point = self.view.size()
        self.view.insert(edit, start_point, text)

        if search_string is not None:
            regions = self.view.find_all(search_string,
                                    sublime.LITERAL | sublime.IGNORECASE)
            self.view.add_regions('whoosh_regions', regions, "text.find-in-files", "", sublime.DRAW_OUTLINED)


class WhooshTestCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global _settings

        _settings = sublime.load_settings(_whoosh_search_settings)
        start = timeit.default_timer()

        print("artemn")

        stop = timeit.default_timer()
        print(stop - start)

