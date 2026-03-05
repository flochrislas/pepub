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
    from pepub import convert_epub
    import pypandoc

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
        self.grid_rowconfigure(2, weight=1)

        # Path row
        path_frame = ctk.CTkFrame(self, fg_color='transparent')
        path_frame.grid(row=0, column=0, padx=16, pady=(16, 4), sticky='ew')
        path_frame.grid_columnconfigure(0, weight=1)

        self.path_var = ctk.StringVar()
        ctk.CTkEntry(
            path_frame, textvariable=self.path_var,
            placeholder_text='Select an EPUB file or a folder...'
        ).grid(row=0, column=0, padx=(0, 8), sticky='ew')
        ctk.CTkButton(
            path_frame, text='File', width=64,
            command=self._browse_file
        ).grid(row=0, column=1, padx=(0, 4))
        ctk.CTkButton(
            path_frame, text='Folder', width=72,
            command=self._browse_folder
        ).grid(row=0, column=2)

        # Options + Convert button row
        ctrl_frame = ctk.CTkFrame(self, fg_color='transparent')
        ctrl_frame.grid(row=1, column=0, padx=16, pady=4, sticky='ew')
        ctrl_frame.grid_columnconfigure(0, weight=1)

        self.overwrite_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            ctrl_frame, text='Overwrite already converted books',
            variable=self.overwrite_var
        ).grid(row=0, column=0, sticky='w')

        self.convert_btn = ctk.CTkButton(
            ctrl_frame, text='Convert', width=120,
            command=self._start
        )
        self.convert_btn.grid(row=0, column=1, padx=(8, 0))

        # Log area
        self.log_box = ctk.CTkTextbox(self, state='disabled', wrap='word')
        self.log_box.grid(row=2, column=0, padx=16, pady=(4, 16), sticky='nsew')

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

        try:
            pypandoc.get_pandoc_version()
        except OSError:
            self._append_log(
                'Error: pandoc is not installed or not found in PATH.\n'
                'Install it from https://pandoc.org/installing.html\n'
            )
            return

        self.convert_btn.configure(state='disabled', text='Converting...')
        self.log_box.configure(state='normal')
        self.log_box.delete('1.0', 'end')
        self.log_box.configure(state='disabled')

        threading.Thread(
            target=self._run,
            args=(path, self.overwrite_var.get()),
            daemon=True
        ).start()

    def _run(self, path, overwrite):
        stream = StreamToQueue(self.log_queue)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = stream
        try:
            target = Path(path)
            if target.is_file():
                convert_epub(target, overwrite=overwrite)
            elif target.is_dir():
                epubs = sorted(target.glob('*.epub'))
                if not epubs:
                    print(f'No EPUB files found in: {target}')
                else:
                    total = len(epubs)
                    for i, epub_path in enumerate(epubs, 1):
                        print(f'[{i}/{total}] {epub_path.name}')
                        try:
                            convert_epub(epub_path, overwrite=overwrite)
                        except Exception as e:
                            print(f'  Error: {e}')
            else:
                print(f'Path not found: {path}')
        except Exception as e:
            print(f'Error: {e}')
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self.after(0, self._on_done)

    def _on_done(self):
        self.convert_btn.configure(state='normal', text='Convert')
        self._append_log('\n--- Done ---\n')


if __name__ == '__main__':
    try:
        app = App()
        app.mainloop()
    except Exception as e:
        tkinter.messagebox.showerror('Startup Error', str(e))
        raise
