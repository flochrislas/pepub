import tkinter
import tkinter.messagebox

try:
    import customtkinter as ctk
    import queue
    import sys
    import threading
    from pathlib import Path
    import tkinter.filedialog as fd

    # Allow importing pepub.py from the same directory as this script
    sys.path.insert(0, str(Path(__file__).parent))
    from pepub import convert_epub, _print_batch_report, sanitize_filename
    import pypandoc
    import io

except Exception as _import_error:
    _root = tkinter.Tk()
    _root.withdraw()
    tkinter.messagebox.showerror('Import Error', str(_import_error))
    raise SystemExit(1)

ctk.set_appearance_mode('system')
ctk.set_default_color_theme('blue')


class StreamToQueue:
    """Redirects print() calls into a queue for thread-safe GUI updates."""
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass

    def isatty(self):
        # pepub's batch report checks this to decide whether to emit ANSI
        # color codes. The GUI textbox doesn't render them, so say False.
        return False


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title('EPUB to Obsidian Converter')
        self.geometry('640x480')
        self.minsize(480, 360)
        self.log_queue = queue.Queue()
        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # Input path row
        path_frame = ctk.CTkFrame(self, fg_color='transparent')
        path_frame.grid(row=0, column=0, padx=16, pady=(16, 4), sticky='ew')
        path_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(path_frame, text='Input:', width=56, anchor='w').grid(
            row=0, column=0, padx=(0, 4))
        self.path_var = ctk.StringVar()
        ctk.CTkEntry(
            path_frame, textvariable=self.path_var,
            placeholder_text='Select an EPUB file or a folder...'
        ).grid(row=0, column=1, padx=(0, 8), sticky='ew')
        ctk.CTkButton(
            path_frame, text='File', width=64,
            command=self._browse_file
        ).grid(row=0, column=2, padx=(0, 4))
        ctk.CTkButton(
            path_frame, text='Folder', width=72,
            command=self._browse_folder
        ).grid(row=0, column=3)

        # Output directory row
        out_frame = ctk.CTkFrame(self, fg_color='transparent')
        out_frame.grid(row=1, column=0, padx=16, pady=4, sticky='ew')
        out_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(out_frame, text='Output:', width=56, anchor='w').grid(
            row=0, column=0, padx=(0, 4))
        self.output_var = ctk.StringVar()
        ctk.CTkEntry(
            out_frame, textvariable=self.output_var,
            placeholder_text='(default: same folder as each EPUB)'
        ).grid(row=0, column=1, padx=(0, 8), sticky='ew')
        ctk.CTkButton(
            out_frame, text='Folder', width=72,
            command=self._browse_output
        ).grid(row=0, column=2)

        # Options + Convert button row
        ctrl_frame = ctk.CTkFrame(self, fg_color='transparent')
        ctrl_frame.grid(row=2, column=0, padx=16, pady=4, sticky='ew')
        ctrl_frame.grid_columnconfigure(0, weight=1)

        self.overwrite_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            ctrl_frame, text='Overwrite already converted books',
            variable=self.overwrite_var,
            command=lambda: self._refresh_preview()
        ).grid(row=0, column=0, sticky='w')

        self.convert_btn = ctk.CTkButton(
            ctrl_frame, text='Convert', width=120,
            command=self._start
        )
        self.convert_btn.grid(row=0, column=1, padx=(8, 0))

        # Shared output area: shows the EPUB file preview before conversion
        # and the live log output during/after conversion.
        self.log_box = ctk.CTkTextbox(self, state='disabled', wrap='word')
        self.log_box.grid(row=3, column=0, padx=16, pady=(4, 16), sticky='nsew')

        # Refresh preview whenever the input or output path changes
        self._converting = False
        self.path_var.trace_add('write', lambda *_: self._refresh_preview())
        self.output_var.trace_add('write', lambda *_: self._refresh_preview())
        self._refresh_preview()

    def _browse_file(self):
        path = fd.askopenfilename(
            title='Select EPUB file',
            filetypes=[('EPUB files', '*.epub'), ('All files', '*.*')]
        )
        if path:
            self.path_var.set(path)

    def _browse_folder(self):
        path = fd.askdirectory(title='Select folder containing EPUBs')
        if path:
            self.path_var.set(path)

    def _browse_output(self):
        path = fd.askdirectory(title='Select output directory')
        if path:
            self.output_var.set(path)

    def _refresh_preview(self):
        """Replace the shared area with a preview of EPUB files at the input path.

        No-op while a conversion is in progress so live log output is not clobbered.
        """
        if self._converting:
            return

        raw = self.path_var.get().strip()
        self.log_box.configure(state='normal')
        self.log_box.delete('1.0', 'end')

        if not raw:
            self.log_box.insert('end', (
                'Welcome to the EPUB to Obsidian Converter.\n'
                '\n'
                'This tool converts EPUB books into folders of Markdown files\n'
                'that can be opened as an Obsidian vault (one file per chapter,\n'
                'images copied to assets/, and an index file with a TOC).\n'
                '\n'
                'How to use:\n'
                '  1. Input (required): click "File" to pick a single .epub,\n'
                '     or "Folder" to batch-convert every .epub inside it.\n'
                '     The list of files that will be converted will appear here.\n'
                '  2. Output (optional): click "Folder" to choose where the\n'
                '     converted books are written. If left empty, each book is\n'
                '     placed next to its source .epub.\n'
                '  3. "Overwrite already converted books": tick this to\n'
                '     re-convert books whose output folder already exists.\n'
                '     Leave it unticked to skip them.\n'
                '  4. Click "Convert". Progress and warnings will be shown\n'
                '     in this area.\n'
                '\n'
                'Requires pandoc to be installed and available in PATH.\n'
            ))
            self.log_box.configure(state='disabled')
            return

        target = Path(raw)
        if not target.exists():
            self.log_box.insert('end', f'Path not found: {target}\n')
            self.log_box.configure(state='disabled')
            return

        overwrite = self.overwrite_var.get()
        out_raw = self.output_var.get().strip()
        # `existing` holds the subdirectory names that already live under the
        # output folder. When overwrite is off, any EPUB whose sanitized stem
        # matches one of these will be skipped by convert_epub, so we hide it
        # from the preview to show exactly what Convert will actually process.
        existing = set()
        if out_raw:
            out_path = Path(out_raw)
            if out_path.is_dir():
                existing = {p.name for p in out_path.iterdir() if p.is_dir()}

        def _will_skip(epub_path):
            if overwrite:
                return False
            # When the user hasn't chosen an output folder, convert_epub writes
            # next to each EPUB — check that EPUB's parent instead.
            folder_name = sanitize_filename(epub_path.stem)
            if out_raw:
                return folder_name in existing
            return (epub_path.parent / folder_name).is_dir()

        if target.is_file():
            if target.suffix.lower() == '.epub':
                if _will_skip(target):
                    self.log_box.insert('end',
                        f'{target.name} — already converted (will be skipped).\n'
                        'Tick "Overwrite already converted books" to re-convert it.\n')
                else:
                    self.log_box.insert('end', f'1 EPUB file selected:\n{target.name}\n')
            else:
                self.log_box.insert('end', 'Selected file is not an EPUB.\n')
        elif target.is_dir():
            epubs = sorted(target.glob('*.epub'))
            total = len(epubs)
            if total == 0:
                self.log_box.insert('end', f'No EPUB files found in: {target}\n')
            else:
                to_convert = [p for p in epubs if not _will_skip(p)]
                skipped = total - len(to_convert)
                plural = 's' if len(to_convert) != 1 else ''
                if skipped:
                    header = (
                        f'{len(to_convert)} of {total} EPUB file{plural} '
                        f'will be converted ({skipped} already converted, skipped).\n'
                        f'Input: {target}\n'
                    )
                else:
                    header = f'{total} EPUB file{plural} will be converted from: {target}\n'
                self.log_box.insert('end', header + '\n')
                if to_convert:
                    self.log_box.insert('end',
                        '\n'.join(p.name for p in to_convert) + '\n')
                else:
                    self.log_box.insert('end',
                        'Nothing to do — every EPUB in this folder has already '
                        'been converted.\nTick "Overwrite already converted '
                        'books" to re-convert them.\n')
        else:
            self.log_box.insert('end', f'Unsupported path: {target}\n')

        self.log_box.configure(state='disabled')

    def _append_log(self, text):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', text)
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def _poll_log_queue(self):
        while True:
            try:
                self._append_log(self.log_queue.get_nowait())
            except queue.Empty:
                break
        self.after(50, self._poll_log_queue)

    def _start(self):
        path = self.path_var.get().strip()
        if not path:
            self._append_log('Please select an EPUB file or folder first.\n')
            return

        output_dir = self.output_var.get().strip() or None
        if output_dir:
            out_path = Path(output_dir)
            if out_path.exists() and not out_path.is_dir():
                self._append_log(f'Error: output path is not a directory: {out_path}\n')
                return
            try:
                out_path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._append_log(f'Error: cannot create output directory: {e}\n')
                return

        try:
            pypandoc.get_pandoc_version()
        except OSError:
            self._append_log(
                'Error: pandoc is not installed or not found in PATH.\n'
                'Install it from https://pandoc.org/installing.html\n'
            )
            return

        self._converting = True
        self.convert_btn.configure(state='disabled', text='Converting...')
        self.log_box.configure(state='normal')
        self.log_box.delete('1.0', 'end')
        self.log_box.configure(state='disabled')

        threading.Thread(
            target=self._run,
            args=(path, self.overwrite_var.get(), output_dir),
            daemon=True
        ).start()

    def _run(self, path, overwrite, output_dir):
        stream = StreamToQueue(self.log_queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = stream
        try:
            target = Path(path)
            if target.is_file():
                convert_epub(target, overwrite=overwrite, output_base_dir=output_dir)
            elif target.is_dir():
                epubs = sorted(target.glob('*.epub'))
                if not epubs:
                    print(f'No EPUB files found in: {target}')
                else:
                    total = len(epubs)
                    results = []

                    class _Tee:
                        def __init__(self, real):
                            self.real = real
                            self.buf = io.StringIO()
                        def write(self, text):
                            self.real.write(text)
                            self.buf.write(text)
                        def flush(self):
                            self.real.flush()

                    for i, epub_path in enumerate(epubs, 1):
                        print(f'[{i}/{total}] {epub_path.name}', flush=True)
                        tee = _Tee(sys.stderr)
                        sys.stderr = tee
                        status = 'ok'
                        error_msg = ''
                        try:
                            outcome = convert_epub(epub_path, overwrite=overwrite,
                                                   output_base_dir=output_dir)
                            if outcome == 'skipped':
                                status = 'skipped'
                        except Exception as e:
                            status = 'error'
                            error_msg = str(e)
                            print(f'  Error: {e}', file=tee.real)
                        finally:
                            sys.stderr = tee.real
                        captured = tee.buf.getvalue()
                        warning_lines = [l for l in captured.splitlines() if l.startswith('Warning:')]
                        results.append((epub_path.name, status, warning_lines, error_msg))

                    _print_batch_report(results)
            else:
                print(f'Path not found: {path}')
        except Exception as e:
            print(f'Error: {e}')
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self.after(0, self._on_done)

    def _on_done(self):
        self._converting = False
        self.convert_btn.configure(state='normal', text='Convert')
        self._append_log('\n--- Done ---\n')


if __name__ == '__main__':
    try:
        app = App()
        app.mainloop()
    except Exception as e:
        tkinter.messagebox.showerror('Startup Error', str(e))
        raise
