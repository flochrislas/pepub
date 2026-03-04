# pepub

Convert EPUB files into markdown files.

I use this to transform a bunch of eBooks in EPUB format into markdown files to use in my Obsidian vault.

It is coded in Python, leveraging the power of Pandoc.

Both command line and GUI are available.

Can work with single files, or by entire folder. There is an option to overwrite existing or not.

Requires the following packages:
`pip install customtkinter ebooklib beautifulsoup4 lxml pyyaml pypandoc`

Requires Pandoc.
On Windows : `winget install --id JohnMacFarlane.Pandoc`, or download from pandoc.org
