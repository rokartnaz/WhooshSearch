# WhooshSearch

   A plugin for [Sublime Text 3](http://www.sublimetext.com/)
   

## About

   WhooshSearch is a ST3 plugin which allows to index sublime projects of any size and to search for any string of text within a project blazingly fast. Search results are presented as a standard ST3 "Find Results" view with an ability to jump into files containing search hits. 

   WhooshSearch uses [Whoosh Search Engine](https://whoosh.readthedocs.io/en/latest/index.html) code under the hood and does not require any additional software for its work. Just install the plugin and enjoy!
   

## Installation

1. Using [Package Control](https://packagecontrol.io/) - Recommended
2. Download [WhooshSearch](https://github.com/rokartnaz/WhooshSearch) repository from github and put it under ST3 Packages folder.


## Usage

1. **[ctrl + shift + i]** / **[super + shift + x]** - build a project index from scratch if it does not exists or incremetally (only files that were changed from last index build will be reindexed).

2. **[ctrl + shift + x]** / **[super + shift + x]** - reset the index.

3. **[ctrl + shift + w]** / **[super + shift + w]** - open WhooshSearch input panel. Use arrows **UP** and **DOWN** to navigate on search history.

4. **[ctrl + s]** / **[super + s]** - saving current file triggers reindexing of the file if it belongs to project.

5. **Whoosh Find Results** - double click on search hit to jump into the file on specific line where hit was located.


   Edit **Default (Windows).sublime-keymap** (Linux or OSX) to change default plugin hotkeys.

   **Note:** WhooshSearch uses status bar (bottom side) to notify users about all its activities.
   

## Settings

```javascript
{
    // Choose folders to skip while indexing
    "skip_folders":
    [
        ".svn",
        ".git",
        ".hg",
        "CVS",
        "CMpub",
        "linux30",
        "linux50"
    ],

    // choose file extensions to skip while indexing
    "skip_file_extensions":
    [
        "txt"
    ],

    // choose file names (without path) to skip while indexing
    "skip_files":
    [
        "WhooshSearch.py"
    ],

    // store files content in index. Allows not to reread files while searching
    "store_content" : true,

    // maximum amount of memory in Mb to use while indexing.
    "ram_limit_mb" : 256
}
```

   Edit **WhooshSearch.sublime-settings** to change the default plugin configuration.

   **Note:** a sublime project won't be reindexed automatically when plugin configuration is changed.

