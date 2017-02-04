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
    import RegexTokenizer, IntraWordFilter, LowercaseFilter, StopFilter, MultiFilter
from WhooshSearch.whoosh.compat import u
from WhooshSearch.whoosh import highlight
from WhooshSearch.whoosh.analysis import Token
from WhooshSearch.whoosh.query import Phrase
from itertools import groupby


_index_folder_tag = ".whoosh"
_whoosh_search_settings = "WhooshSearch.sublime-settings"
_settings = None
_whoosh_view_id = 4294967295
_whoosh_syntax_file = "Packages/WhooshSearch/WhooshFindResults.hidden-tmLanguage"

def custom_analyzer():
    delims = u("_- '\"()!@#$%^&*[]{}<>\|;:,./?`~=+")
    intraword = MultiFilter(index=IntraWordFilter(splitwords=True,
                                                  splitnums=True,
                                                  mergewords=True,
                                                  mergenums=True,
                                                  delims=delims),
                            query=IntraWordFilter(splitwords=False,
                                                  splitnums=False,
                                                  delims=delims))

    # return RegexTokenizer(expression=r"\s+", gaps=True) \
    #        | IntraWordFilter(splitwords=False,
    #                          splitnums=False,
    #                          delims=delims) \
    #        | LowercaseFilter() \
    #        | StopFilter()

    return analysis.StandardAnalyzer()


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

    # if is_binary_file(fname):
    #     return False

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
    all_files = []

    for folder in project_folders():
        folder_files = []
        for (dirpath, dirnames, filenames) in os.walk(folder, topdown=True):
            dirnames[:] = [d for d in dirnames if dir_filter(d)]

            for f in filenames:
                fname = os.path.join(dirpath, f)
                if file_filter(fname):
                    folder_files.append(fname)

        all_files.extend(folder_files)

    print("artemn: project_files: %d" % len(all_files))

    return all_files


def artemn_project_files():
    all_files = []
    count = 0

    for folder in project_folders():
        for (dirpath, dirnames, filenames) in os.walk(folder, topdown=True):
            dirnames[:] = [d for d in dirnames if dir_filter(d)]

            for f in filenames:
                fname = os.path.join(dirpath, f)
                # if file_filter(fname):
                count += 1

        #all_files.extend(folder_files)

    print("artemn: project_files: %d" % count)

    return all_files


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
    with ix.writer() as writer:
        for fname in project_files():
            add_doc_to_index(writer, fname)
    return ix


# TODO should consider changes of settings to filter files and folders
def incremental_index(index_path):
    ix = index.open_dir(index_path)

    # The set of all paths in the index
    indexed_paths = set()
    # The set of all paths we need to re-index
    to_index = set()

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
            for path in project_files():
                if path in to_index or path not in indexed_paths:
                    # This is either a file that's changed, or a new file
                    # that wasn't indexed before. So index it!
                    add_doc_to_index(writer, path)
    return ix


def display_search_results(whoosh_view, row_col, file_path, fragments):
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
    except WhooshSearch.whoosh.store.LockError:
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
    except WhooshSearch.whoosh.store.LockError:
        print("Index is locked") 


def whoosh_search(search_string):
    start = timeit.default_timer()
    print("ARTEMN!!!!!!!!!")

    if not is_project():
        print("WhooshSearch searches only projects")
        return

    if not index.exists_in(index_folder()):
        print("WhooshSearch: Please create the index")
        return

    ix = index.open_dir(index_folder())
    qp = QueryParser("content", schema=ix.schema)

    # Search for phrases. search_string should to be in quotes
    q = qp.parse('"%s"' % search_string)

    with ix.searcher() as searcher:
        hits = searcher.search(q, limit=None, terms=True)
        show_hits(hits)

    stop = timeit.default_timer()
    print(stop - start)


def setup_hits(hits):
    hits.highlighter = CustomHighlighter()
    hits.fragmenter = CustomPinpointFragmenter()
    hits.fragmenter.charlimit = None
    hits.formatter = CustomFormatter()


def create_whoosh_view():
    whoosh_view = sublime.active_window().new_file()
    whoosh_view.set_scratch(True)
    whoosh_view.set_name("Whoosh Find Results")
    whoosh_view.set_syntax_file(_whoosh_syntax_file)
    #whoosh_view.set_read_only(True)


def show_hits(hits):
    print("ARTEMN %d" % len(hits))

    setup_hits(hits)
    whoosh_view = create_whoosh_view()

    row_col = (0, 0)
    for hit in hits:
        content = file_content(hit["path"])
        fragments = hit.highlights("content", text=content, top=1)
        row_col = display_search_results(whoosh_view, row_col, hit["path"], fragments)

###############################################################################

class WhooshIndexCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        sublime.set_timeout_async(whoosh_index, 1)


class WhooshSearchCommand(sublime_plugin.TextCommand):
    def run(self, edit, search_string="artemn my_function123"):
        sublime.set_timeout_async(lambda: whoosh_search(search_string), 1)


class WhooshResetCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        sublime.set_timeout_async(whoosh_reset, 1)


class WhooshTestCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global _settings

        _settings = sublime.load_settings(_whoosh_search_settings)
        start = timeit.default_timer()

        qp = QueryParser("content", schema=get_schema())

        # Search for phrases. search_string should to be in quotes
        search_string = "my_function artemn"
        q = qp.parse('"%s"' % search_string)
        print(q)

        stop = timeit.default_timer()
        print(stop - start)

