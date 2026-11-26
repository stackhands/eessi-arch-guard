/*
 * eessi_arch_guard._launch: replace the process image with execv(2).
 *
 * The module holds no state, so it needs no lock and runs unchanged with or
 * without the GIL. With Py_LIMITED_API defined it calls only functions of the
 * stable ABI, all present since CPython 3.2 (PyModuleDef_Init since 3.5).
 */
#include <Python.h>

#include <errno.h>
#include <string.h>
#include <unistd.h>

#if defined(Py_GIL_DISABLED) && !defined(Py_mod_gil)
#error "free-threaded headers without Py_mod_gil"
#endif

PyDoc_STRVAR(execv_doc,
"execv(path, argv)\n"
"--\n\n"
"Replace the current process with the program at path, run with the\n"
"arguments in argv. path and every element of argv are str, bytes or\n"
"os.PathLike. Does not return on success; raises OSError on failure.");

static PyObject *
launch_execv(PyObject *self, PyObject *args)
{
    PyObject *path_obj = NULL;
    PyObject *argv_obj = NULL;
    PyObject *path_bytes = NULL;
    PyObject **held = NULL;
    char **cargv = NULL;
    Py_ssize_t n = 0;
    Py_ssize_t filled = 0;

    (void)self;
    if (!PyArg_ParseTuple(args, "OO:execv", &path_obj, &argv_obj)) {
        return NULL;
    }
    if (PyUnicode_Check(argv_obj) || PyBytes_Check(argv_obj) || !PySequence_Check(argv_obj)) {
        PyErr_SetString(PyExc_TypeError, "execv() arg 2 must be a sequence of paths");
        return NULL;
    }
    n = PySequence_Size(argv_obj);
    if (n < 0) {
        return NULL;
    }
    if (n == 0) {
        PyErr_SetString(PyExc_ValueError, "execv() arg 2 must not be empty");
        return NULL;
    }
    if (!PyUnicode_FSConverter(path_obj, &path_bytes)) {
        return NULL;
    }
    if ((size_t)n > (size_t)PY_SSIZE_T_MAX / sizeof(char *) - 1) {
        PyErr_NoMemory();
        goto done;
    }
    held = (PyObject **)PyMem_Malloc((size_t)n * sizeof(PyObject *));
    cargv = (char **)PyMem_Malloc(((size_t)n + 1) * sizeof(char *));
    if (held == NULL || cargv == NULL) {
        PyErr_NoMemory();
        goto done;
    }
    memset(held, 0, (size_t)n * sizeof(PyObject *));
    memset(cargv, 0, ((size_t)n + 1) * sizeof(char *));
    for (; filled < n; filled++) {
        PyObject *item = PySequence_GetItem(argv_obj, filled);
        if (item == NULL) {
            goto done;
        }
        int converted = PyUnicode_FSConverter(item, &held[filled]);
        Py_DECREF(item);
        if (!converted) {
            goto done;
        }
        cargv[filled] = PyBytes_AsString(held[filled]);
        if (cargv[filled] == NULL) {
            filled++;
            goto done;
        }
    }
    cargv[n] = NULL;

    (void)execv(PyBytes_AsString(path_bytes), cargv);
    PyErr_SetFromErrnoWithFilenameObject(PyExc_OSError, path_obj);

done:
    for (Py_ssize_t i = 0; i < filled; i++) {
        Py_XDECREF(held[i]);
    }
    PyMem_Free(held);
    PyMem_Free(cargv);
    Py_XDECREF(path_bytes);
    return NULL;
}

static PyMethodDef launch_methods[] = {
    {"execv", launch_execv, METH_VARARGS, execv_doc},
    {NULL, NULL, 0, NULL},
};

static PyModuleDef_Slot launch_slots[] = {
#ifdef Py_mod_multiple_interpreters
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
#endif
#ifdef Py_mod_gil
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
#endif
    {0, NULL},
};

static struct PyModuleDef launch_module = {
    PyModuleDef_HEAD_INIT,
    "eessi_arch_guard._launch",
    "Replace the process with a program: execv(2) for the launcher.",
    0,
    launch_methods,
    launch_slots,
    NULL,
    NULL,
    NULL,
};

PyMODINIT_FUNC
PyInit__launch(void)
{
    return PyModuleDef_Init(&launch_module);
}
