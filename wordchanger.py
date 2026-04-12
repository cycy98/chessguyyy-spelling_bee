import json
import os
import tkinter as tk
from tkinter import ttk, messagebox

LEVELS = [
    "starter",
    "easy",
    "tricky",
    "advanced",
    "insane",
    "expert",
    "master",
    "sesquipedalian"
]

class SpellingBeeManager:
    def __init__(self, filename="wordlist.json"):
        self.filename = filename
        self.data = self.load()

    def load(self):
        if not os.path.exists(self.filename):
            return {level: {} for level in LEVELS}
        with open(self.filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            for level in LEVELS:
                if level not in data or not isinstance(data[level], dict):
                    data[level] = {}
            return data

    def save(self):
        self.sort_all()
        lines = ["{"]
        all_keys = LEVELS + [k for k in self.data.keys() if k not in LEVELS]
        for i, level in enumerate(all_keys):
            if level not in self.data: continue
            lines.append(f'    "{level}": {{')
            words = list(self.data[level].items())
            for j, (word, details) in enumerate(words):
                details_json = json.dumps(details, ensure_ascii=False)
                comma = "," if j < len(words) - 1 else ""
                lines.append(f'        "{word}": {details_json}{comma}')
            level_comma = "," if i < len(all_keys) - 1 else ""
            lines.append(f'    }}{level_comma}')
        lines.append("}")
        with open(self.filename, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def sort_all(self):
        for level in LEVELS:
            if level not in self.data: continue
            for word in list(self.data[level].keys()):
                entry = self.data[level].get(word, {})
                if not isinstance(entry, dict):
                    entry = {"part_of_speech": "", "definition": "", "homophones": []}
                if "homophones" not in entry or not isinstance(entry["homophones"], list):
                    entry["homophones"] = []
                entry["homophones"].sort(key=str.lower)
                self.data[level][word] = entry
            self.data[level] = dict(sorted(self.data[level].items(), key=lambda x: x[0].lower()))

    def find_word(self, word):
        for level in LEVELS:
            if word in self.data[level]: return level
        return None

    def add_word(self, level, word, pos, definition):
        if self.find_word(word): raise ValueError("Word already exists")
        self.data[level][word] = {"part_of_speech": pos, "definition": definition, "homophones": []}
        self.save()

    def add_homophone(self, word, homophone):
        level = self.find_word(word)
        if not level: raise ValueError("Word not found")
        if homophone not in self.data[level][word]["homophones"]:
            self.data[level][word]["homophones"].append(homophone)
        self.save()

    def promote_word(self, word):
        level = self.find_word(word)
        if not level: raise ValueError("Word not found")
        index = LEVELS.index(level)
        if index == len(LEVELS) - 1: raise ValueError("Already highest level")
        new_level = LEVELS[index + 1]
        self.data[new_level][word] = self.data[level].pop(word)
        self.save()

    def demote_word(self, word):
        level = self.find_word(word)
        if not level: raise ValueError("Word not found")
        index = LEVELS.index(level)
        if index == 0: raise ValueError("Already lowest level")
        new_level = LEVELS[index - 1]
        self.data[new_level][word] = self.data[level].pop(word)
        self.save()

class App:
    def __init__(self, root):
        self.manager = SpellingBeeManager()
        self.root = root
        self.root.title("Spelling Bee Manager")
        self.create_layout()
        self.refresh_word_list()

    def create_layout(self):
        left = ttk.Frame(self.root)
        left.grid(row=0, column=0, padx=10, pady=10)
        right = ttk.Frame(self.root)
        right.grid(row=0, column=1, padx=10, pady=10, sticky="n")

        # Level selector
        ttk.Label(left, text="Level").pack()
        self.level_var = tk.StringVar(value=LEVELS[0])
        self.level_menu = ttk.Combobox(left, textvariable=self.level_var, values=LEVELS)
        self.level_menu.pack()
        self.level_menu.bind("<<ComboboxSelected>>", lambda e: self.refresh_word_list())

        # Search bar
        ttk.Label(left, text="Search").pack(pady=(10, 0))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *args: self.refresh_word_list())
        ttk.Entry(left, textvariable=self.search_var).pack()

        # Word list
        self.word_list = tk.Listbox(left, width=30, height=20)
        self.word_list.pack(pady=5)
        self.word_list.bind("<<ListboxSelect>>", lambda e: self.show_details())

        # Action Buttons
        ttk.Button(left, text="Promote", command=self.promote_word).pack(fill="x")
        ttk.Button(left, text="Demote", command=self.demote_word).pack(fill="x")
        
        # --- NEW COPY BUTTONS ---
        ttk.Separator(left, orient='horizontal').pack(fill='x', pady=10)
        ttk.Button(left, text="Copy Current Level", command=self.copy_current_level).pack(fill="x")
        ttk.Button(left, text="Copy All (A-Z)", command=self.copy_all_alphabetical).pack(fill="x")
        ttk.Button(left, text="Copy All (By Difficulty)", command=self.copy_all_by_difficulty).pack(fill="x")

        # Right panel details
        ttk.Label(right, text="Word Details", font=("Arial", 12, "bold")).pack()
        self.details_text = tk.Text(right, width=50, height=15, wrap="word")
        self.details_text.pack()

        # Add word section
        ttk.Label(right, text="Add New Word").pack(pady=(10, 0))
        self.new_word = ttk.Entry(right); self.new_word.pack()
        self.new_pos = ttk.Entry(right); self.new_pos.pack()
        self.new_def = ttk.Entry(right); self.new_def.pack()
        ttk.Button(right, text="Add Word", command=self.add_word).pack(pady=5)

        # Add homophone
        ttk.Label(right, text="Add Homophone").pack()
        self.new_hom = ttk.Entry(right); self.new_hom.pack()
        ttk.Button(right, text="Add Homophone", command=self.add_homophone).pack()

    def refresh_word_list(self):
        self.word_list.delete(0, tk.END)
        level = self.level_var.get()
        search = self.search_var.get().lower()
        for word in self.manager.data[level]:
            if search in word.lower():
                self.word_list.insert(tk.END, word)

    # --- COPY LOGIC ---
    def copy_current_level(self):
        level = self.level_var.get()
        words = sorted(list(self.manager.data[level].keys()), key=str.lower)
        if not words:
            messagebox.showinfo("Empty", f"No words in {level} level.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(words))
        messagebox.showinfo("Copied", f"Copied {len(words)} words from '{level}' to clipboard.")

    def copy_all_alphabetical(self):
        all_words = []
        for level in LEVELS:
            all_words.extend(self.manager.data[level].keys())
        
        if not all_words:
            messagebox.showinfo("Empty", "The total wordlist is empty.")
            return
            
        all_words.sort(key=str.lower)
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(all_words))
        messagebox.showinfo("Copied", f"Copied all {len(all_words)} words (A-Z) to clipboard.")

    def copy_all_by_difficulty(self):
        total_words = sum(len(self.manager.data[level]) for level in LEVELS)
        if total_words == 0:
            messagebox.showinfo("Empty", "The total wordlist is empty.")
            return

        lines = []
        for level in LEVELS:
            words = sorted(self.manager.data[level].keys(), key=str.lower)
            lines.append(f"--{level}--")
            lines.extend(words)
            lines.append("")

        if lines and lines[-1] == "":
            lines.pop()

        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))
        messagebox.showinfo("Copied", f"Copied all {total_words} words grouped by difficulty.")

    def get_selected_word(self):
        selection = self.word_list.curselection()
        return self.word_list.get(selection[0]) if selection else None

    def show_details(self):
        word = self.get_selected_word()
        if not word: return
        level = self.manager.find_word(word)
        self.details_text.delete("1.0", tk.END)
        if not level:
            self.details_text.insert(tk.END, f"Word: {word}\nLevel: (unknown)")
            return
        entry = self.manager.data[level][word]
        self.details_text.insert(tk.END, f"Word: {word}\nLevel: {level}\nPart of Speech: {entry['part_of_speech']}\n\n")
        self.details_text.insert(tk.END, f"Definition:\n{entry['definition']}\n\nHomophones:\n")
        for h in entry["homophones"]: self.details_text.insert(tk.END, f"  - {h}\n")

    def add_word(self):
        try:
            self.manager.add_word(self.level_var.get(), self.new_word.get(), self.new_pos.get(), self.new_def.get())
            self.refresh_word_list()
        except ValueError as e: messagebox.showerror("Error", str(e))

    def add_homophone(self):
        word = self.get_selected_word()
        if not word: return
        try:
            self.manager.add_homophone(word, self.new_hom.get())
            self.show_details()
        except ValueError as e: messagebox.showerror("Error", str(e))

    def promote_word(self):
        word = self.get_selected_word()
        if not word: return
        try:
            self.manager.promote_word(word)
            self.refresh_word_list()
        except ValueError as e: messagebox.showerror("Error", str(e))

    def demote_word(self):
        word = self.get_selected_word()
        if not word: return
        try:
            self.manager.demote_word(word)
            self.refresh_word_list()
        except ValueError as e: messagebox.showerror("Error", str(e))

if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
