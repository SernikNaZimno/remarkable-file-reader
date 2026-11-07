@echo off
rem Uruchamia Czytnik reMarkable bez okna konsoli.
rem Mozna tez przeciagnac plik .rmdoc na ten plik .bat.
start "" pythonw "%~dp0remarkable_reader.py" %*
