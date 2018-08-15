# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

from __future__ import print_function
import tensorflow as tf
import edgeml.utils as utils
import numpy as np
import os
import sys


class FastTrainer:

    def __init__(self, FastObj, X, Y, sW=1.0, sU=1.0, learningRate=0.01,
                 outFile=None):
        '''
        FastObj - Can be either FastRNN or FastGRNN with proper initialisations
        sW and sU are the sparsity factors for Fast parameters
        X is the Data Placeholder - Dims [_, timesteps, input_dims]
        Y is the label placeholder for loss computation - Dims [_, num_classes]
        batchSize is the batchSize
        learningRate is the initial learning rate
        '''

        self.FastObj = FastObj

        self.sW = sW
        self.sU = sU

        self.Y = Y
        self.X = X

        self.numClasses = int(self.Y.shape[1])
        self.timeSteps = int(self.X.shape[1])
        self.inputDims = int(self.X.shape[2])

        self.learningRate = learningRate

        if outFile is not None:
            self.outFile = open(outFile, 'w')
        else:
            self.outFile = sys.stdout

        self.lr = tf.placeholder("float")

        self.logits, self.finalHiddenState, self.predictions = self.computeGraph()

        self.lossOp = self.lossGraph(self.logits, self.Y)
        self.trainOp = self.trainGraph(self.lossOp, self.lr)

        self.correctPredictions, self.accuracy = self.accuracyGraph(
            self.predictions, self.Y)

        self.numMatrices = self.FastObj.num_weight_matrices()
        self.totalMatrices = self.numMatrices[0] + self.numMatrices[1]

        self.FastParams = self.FastObj.getVars()

        if self.sW > 0.99 and self.sU > 0.99:
            self.isDenseTraining = True
        else:
            self.isDenseTraining = False

        self.hardThrsdGraph()
        self.sparseTrainingGraph()

    def RNN(self, x, timeSteps, FastObj):
        '''
        Unrolls and adds linear classifier
        '''
        x = tf.unstack(x, timeSteps, 1)
        outputs, states = tf.nn.static_rnn(FastObj, x, dtype=tf.float32)
        return outputs[-1]

    def computeGraph(self):
        '''
        Compute graph to unroll and predict on the FastObj
        '''
        finalHiddenState = self.RNN(self.X, self.timeSteps, self.FastObj)

        logits = self.classifier(finalHiddenState)
        predictions = tf.nn.softmax(logits)

        return logits, finalHiddenState, predictions

    def classifier(self, feats):
        '''
        Can be raplaced by any classifier
        TODO: Make this a separate class if needed
        '''
        self.FC = tf.Variable(tf.random_normal(
            [self.FastObj.output_size(), self.numClasses]), name='FC')
        self.FCbias = tf.Variable(tf.random_normal(
            [self.numClasses]), name='FCbias')

        return tf.matmul(feats, self.FC) + self.FCbias

    def lossGraph(self, logits, Y):
        '''
        Loss Graph for given FastObj
        '''
        lossOp = utils.crossEntropyLoss(logits, Y)
        return lossOp

    def trainGraph(self, lossOp, lr):
        '''
        Train Graph for the loss generated by Bonsai
        '''
        optimizer = tf.train.AdamOptimizer(lr)
        trainOp = optimizer.minimize(lossOp)
        return trainOp

    def accuracyGraph(self, predictions, Y):
        '''
        Accuracy Graph to evaluate accuracy when needed
        '''
        correctPredictions = tf.equal(
            tf.argmax(predictions, 1), tf.argmax(Y, 1))
        accuracy = tf.reduce_mean(tf.cast(correctPredictions, tf.float32))
        return correctPredictions, accuracy

    def assertInit(self):
        err = "sparsity must be between 0 and 1"
        assert self.sW >= 0 and self.sW <= 1, "W " + err
        assert self.sU >= 0 and self.sU <= 1, "U " + err

    def hardThrsdGraph(self):
        '''
        Set up for hard Thresholding Functionality
        '''
        self.paramPlaceholders = []
        self.htOps = []
        for i in range(0, self.numMatrices[0]):
            self.paramPlaceholders.append(tf.placeholder(
                tf.float32, name="Wth_" + str(i)))
        for i in range(self.numMatrices[0], self.totalMatrices):
            self.paramPlaceholders.append(tf.placeholder(
                tf.float32, name="Uth_" + str(i)))

        for i in range(0, self.numMatrices[0]):
            self.htOps.append(
                self.FastParams[i].assign(self.paramPlaceholders[i]))
        for i in range(self.numMatrices[0], self.totalMatrices):
            self.htOps.append(
                self.FastParams[i].assign(self.paramPlaceholders[i]))

        self.hardThresholdGroup = tf.group(*self.htOps)

    def sparseTrainingGraph(self):
        '''
        Set up for Sparse Retraining Functionality
        '''
        self.stOps = []

        for i in range(0, self.numMatrices[0]):
            self.stOps.append(
                self.FastParams[i].assign(self.paramPlaceholders[i]))
        for i in range(self.numMatrices[0], self.totalMatrices):
            self.stOps.append(
                self.FastParams[i].assign(self.paramPlaceholders[i]))

        self.sparseRetrainGroup = tf.group(*self.stOps)

    def runHardThrsd(self, sess):
        '''
        Function to run the IHT routine on FastObj
        '''
        self.thrsdParams = []
        for i in range(0, self.numMatrices[0]):
            self.thrsdParams.append(
                utils.hardThreshold(self.FastParams[i].eval(), self.sW))
        for i in range(self.numMatrices[0], self.totalMatrices):
            self.thrsdParams.append(
                utils.hardThreshold(self.FastParams[i].eval(), self.sU))

        fd_thrsd = {}
        for i in range(0, self.totalMatrices):
            fd_thrsd[self.paramPlaceholders[i]] = self.thrsdParams[i]
        sess.run(self.hardThresholdGroup, feed_dict=fd_thrsd)

    def runSparseTraining(self, sess):
        '''
        Function to run the Sparse Retraining routine on FastObj
        '''
        self.reTrainParams = []
        for i in range(0, self.totalMatrices):
            self.reTrainParams.append(
                utils.copySupport(self.thrsdParams[i], self.FastParams[i].eval()))

        fd_st = {}
        for i in range(0, self.totalMatrices):
            fd_st[self.paramPlaceholders[i]] = self.reTrainParams[i]
        sess.run(self.sparseRetrainGroup, feed_dict=fd_st)

    def getModelSize(self):
        '''
        Function to get aimed model size
        '''
        totalnnZ = 0
        totalSize = 0
        hasSparse = False
        for i in range(0, self.numMatrices[0]):
            nnz, size, sparseFlag = utils.countnnZ(self.FastParams[i], self.sW)
            totalnnZ += nnz
            totalSize += size
            hasSparse = hasSparse or sparseFlag

        for i in range(self.numMatrices[0], self.totalMatrices):
            nnz, size, sparseFlag = utils.countnnZ(self.FastParams[i], self.sU)
            totalnnZ += nnz
            totalSize += size
            hasSparse = hasSparse or sparseFlag
        for i in range(self.totalMatrices, len(self.FastParams)):
            nnz, size, sparseFlag = utils.countnnZ(self.FastParams[i], 1.0)
            totalnnZ += nnz
            totalSize += size
            hasSparse = hasSparse or sparseFlag

        # Replace this with classifier class call
        nnz, size, sparseFlag = utils.countnnZ(self.FC, 1.0)
        totalnnZ += nnz
        totalSize += size
        hasSparse = hasSparse or sparseFlag

        nnz, size, sparseFlag = utils.countnnZ(self.FCbias, 1.0)
        totalnnZ += nnz
        totalSize += size
        hasSparse = hasSparse or sparseFlag

        return totalnnZ, totalSize, hasSparse

    def saveParams(self, currDir):
        '''
        Function to save Parameter matrices
        '''
        if self.numMatrices[0] == 1:
            np.save(currDir + '/W.npy', self.FastParams[0].eval())
        else:
            np.save(currDir + '/W1.npy', self.FastParams[0].eval())
            np.save(currDir + '/W2.npy', self.FastParams[1].eval())

        if self.numMatrices[1] == 1:
            np.save(currDir + '/U.npy',
                    self.FastParams[self.numMatrices[0]].eval())
        else:
            np.save(currDir + '/U1.npy',
                    self.FastParams[self.numMatrices[0]].eval())
            np.save(currDir + '/U2.npy',
                    self.FastParams[self.numMatrices[0] + 1].eval())

        if self.FastObj.cellType() == "FastGRNN":
            np.save(currDir + '/Bg.npy',
                    self.FastParams[self.totalMatrices].eval())
            np.save(currDir + '/Bh.npy',
                    self.FastParams[self.totalMatrices + 1].eval())
            np.save(currDir + '/zeta.npy',
                    self.FastParams[self.totalMatrices + 2].eval())
            np.save(currDir + '/nu.npy',
                    self.FastParams[self.totalMatrices + 3].eval())
        elif self.FastObj.cellType() == "FastRNN":
            np.save(currDir + '/B.npy',
                    self.FastParams[self.totalMatrices].eval())
            np.save(currDir + '/alpha.npy', self.FastParams[
                    self.totalMatrices + 1].eval())
            np.save(currDir + '/beta.npy',
                    self.FastParams[self.totalMatrices + 2].eval())
        np.save(currDir + '/FC.npy', self.FC.eval())
        np.save(currDir + '/FCbias.npy', self.FCbias.eval())

    def train(self, batchSize, totalEpochs, sess,
              Xtrain, Xtest, Ytrain, Ytest,
              decayStep, decayRate, dataDir, currDir):
        '''
        The Dense - IHT - Sparse Retrain Routine for FastCell Training
        '''
        resultFile = open(
            dataDir + '/' + str(self.FastObj.cellType()) + 'Results.txt', 'a+')
        numIters = Xtrain.shape[0] / batchSize
        totalBatches = numIters * totalEpochs

        counter = 0
        trimlevel = 15
        ihtDone = 0
        if self.isDenseTraining is True:
            ihtDone = 1
            maxTestAcc = -10000
        header = '*' * 20

        for i in range(0, totalEpochs):
            print("\nEpoch Number: " + str(i), file=self.outFile)

            if i % decayStep == 0 and i != 0:
                self.learningRate = self.learningRate * decayRate

            trainAcc = 0.0
            trainLoss = 0.0

            numIters = int(numIters)
            for j in range(0, numIters):

                if counter == 0:
                    msg = " Dense Training Phase Started "
                    print("\n%s%s%s\n" %
                          (header, msg, header), file=self.outFile)

                batchX = Xtrain[j * batchSize:(j + 1) * batchSize]
                batchY = Ytrain[j * batchSize:(j + 1) * batchSize]
                batchX = batchX.reshape(
                    (batchSize, self.timeSteps, self.inputDims))

                # Mini-batch training
                _, batchLoss, batchAcc = sess.run([self.trainOp, self.lossOp, self.accuracy], feed_dict={
                                                  self.X: batchX, self.Y: batchY, self.lr: self.learningRate})

                trainAcc += batchAcc
                trainLoss += batchLoss

                # Training routine involving IHT and sparse retraining
                if (counter >= int(totalBatches / 3.0) and
                        (counter < int(2 * totalBatches / 3.0)) and
                        counter % trimlevel == 0 and
                        self.isDenseTraining is False):
                    self.runHardThrsd(sess)
                    if ihtDone == 0:
                        msg = " IHT Phase Started "
                        print("\n%s%s%s\n" %
                              (header, msg, header), file=self.outFile)
                    ihtDone = 1
                elif ((ihtDone == 1 and counter >= int(totalBatches / 3.0) and
                       (counter < int(2 * totalBatches / 3.0)) and
                       counter % trimlevel != 0 and
                       self.isDenseTraining is False) or
                        (counter >= int(2 * totalBatches / 3.0) and
                            self.isDenseTraining is False)):
                    self.runSparseTraining(sess)
                    if counter == int(2 * totalBatches / 3.0):
                        msg = " Sprase Retraining Phase Started "
                        print("\n%s%s%s\n" %
                              (header, msg, header), file=self.outFile)
                counter += 1

            print("Train Loss: " + str(trainLoss / numIters) +
                  " Train Accuracy: " + str(trainAcc / numIters),
                  file=self.outFile)

            testData = Xtest.reshape((-1, self.timeSteps, self.inputDims))

            testAcc, testLoss = sess.run([self.accuracy, self.lossOp], feed_dict={
                                         self.X: testData, self.Y: Ytest})

            if ihtDone == 0:
                maxTestAcc = -10000
                maxTestAccEpoch = i
            else:
                if maxTestAcc <= testAcc:
                    maxTestAccEpoch = i
                    maxTestAcc = testAcc

            print("Test Loss: " + str(testLoss) +
                  " Test Accuracy: " + str(testAcc), file=self.outFile)
            self.outFile.flush()

        print("\nMaximum Test accuracy at compressed" +
              " model size(including early stopping): " +
              str(maxTestAcc) + " at Epoch: " +
              str(maxTestAccEpoch + 1) + "\nFinal Test" +
              " Accuracy: " + str(testAcc), file=self.outFile)
        print("\n\nNon-Zeros: " + str(self.getModelSize()[0]) + " Model Size: " +
              str(float(self.getModelSize()[1]) / 1024.0) + " KB hasSparse: " +
              str(self.getModelSize()[2]) + "\n", file=self.outFile)

        resultFile.write("MaxTestAcc: " + str(maxTestAcc) +
                         " at Epoch(totalEpochs): " +
                         str(maxTestAccEpoch + 1) +
                         "(" + str(totalEpochs) + ")" + " ModelSize: " +
                         str(float(self.getModelSize()[1]) / 1024.0) +
                         " KB hasSparse: " + str(self.getModelSize()[2]) +
                         " Param Directory: " +
                         str(os.path.abspath(currDir)) + "\n")

        self.saveParams(currDir)
        print("The Model Directory: " + currDir + "\n")

        resultFile.close()
        self.outFile.flush()
        self.outFile.close()
